from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple


BRIDGE_VERSION = "0.1.1-alpha.1"
PROVENANCE_FILENAME = "timeline_bridge_provenance.json"

# Only builds that have completed the local DLL crypto round-trip and Jianying UI
# open/save/reopen acceptance test belong here. An application update must stop
# conversion until a new calibration adds its exact DLL hash.
SUPPORTED_JIANYING_BUILDS: Dict[str, str] = {
    "11.2.0.14339": "654371a6dca840f53ae786df43fc345365e54d2189d5572ce7c4658aab23f540",
}

_WINDOWS_INVALID_BASENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class SafetyGateError(RuntimeError):
    """Raised when a conversion cannot be proven safe enough to continue."""


@dataclass(frozen=True)
class JianyingBuild:
    version: str
    install_dir: Path
    dll_path: Path
    dll_sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sampled_media_fingerprint(path: Path, sample_size: int = 1024 * 1024) -> Dict[str, object]:
    """Return a cheap identity fingerprint without hashing a multi-gigabyte source in full."""

    stat = path.stat()
    size = stat.st_size
    offsets = sorted({0, max(0, size // 2 - sample_size // 2), max(0, size - sample_size)})
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            data = handle.read(min(sample_size, max(0, size - offset)))
            digest.update(offset.to_bytes(8, "little", signed=False))
            digest.update(len(data).to_bytes(8, "little", signed=False))
            digest.update(data)
    return {
        "algorithm": "sha256-size-first-middle-last-1MiB-v1",
        "digest": digest.hexdigest(),
        "size_bytes": size,
        "mtime_ns": stat.st_mtime_ns,
    }


def validate_draft_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise SafetyGateError("草稿名称不能为空")
    if name != name.strip() or name.endswith((".", " ")):
        raise SafetyGateError("草稿名称不能有首尾空格，也不能以句点结尾")
    if name in {".", ".."} or _WINDOWS_INVALID_BASENAME.search(name):
        raise SafetyGateError("草稿名称必须是单层安全名称，不能包含路径或 Windows 禁用字符")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise SafetyGateError("草稿名称是 Windows 保留名称")
    if len(name) > 120:
        raise SafetyGateError("草稿名称过长（最多 120 个字符）")
    return name


def guarded_child(root: Path, name: str) -> Path:
    validate_draft_name(name)
    resolved_root = root.resolve()
    target = (resolved_root / name).resolve()
    if target.parent != resolved_root:
        raise SafetyGateError("输出目标越过了指定根目录")
    return target


def ensure_new_output(path: Path, *, kind: str = "输出") -> None:
    if path.exists():
        raise FileExistsError(f"{kind}已经存在，工具不会覆盖：{path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"{kind}的父目录不存在：{path.parent}")


def detect_supported_build(install_dir: Path) -> JianyingBuild:
    install_dir = install_dir.resolve()
    dll_path = install_dir / "videoeditor.dll"
    if not dll_path.is_file():
        raise FileNotFoundError(f"找不到剪映加密组件：{dll_path}")
    version = install_dir.name
    actual_hash = sha256_file(dll_path)
    expected_hash = SUPPORTED_JIANYING_BUILDS.get(version)
    if expected_hash is None:
        raise SafetyGateError(
            f"剪映版本 {version} 尚未完成兼容性校准；为避免生成坏工程，本次已停止"
        )
    if actual_hash.lower() != expected_hash.lower():
        raise SafetyGateError(
            f"剪映 {version} 的核心文件与已验证版本不一致；为避免误解密或误写入，本次已停止"
        )
    return JianyingBuild(version, install_dir, dll_path, actual_hash)


def jianying_is_running() -> bool:
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return "JianyingPro.exe" in completed.stdout


def require_jianying_closed() -> None:
    if jianying_is_running():
        raise SafetyGateError("剪映仍在运行。请完全退出剪映后再转换，以避免读取或写入半保存工程")


def stable_read_bytes(path: Path, *, delay_seconds: float = 0.15) -> Tuple[bytes, str]:
    """Read twice and reject a file that is changing under us."""

    first_stat = path.stat()
    first = path.read_bytes()
    time.sleep(delay_seconds)
    second_stat = path.stat()
    second = path.read_bytes()
    if (
        first_stat.st_size != second_stat.st_size
        or first_stat.st_mtime_ns != second_stat.st_mtime_ns
        or first != second
    ):
        raise SafetyGateError(f"文件正在变化，无法取得稳定快照：{path}")
    return second, hashlib.sha256(second).hexdigest()


def atomic_write_new(path: Path, data: bytes) -> None:
    """Create a new file atomically and never replace an existing output."""

    ensure_new_output(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError(f"输出在写入期间被创建，工具不会覆盖：{path}")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def find_latest_supported_install(product_root: Path) -> Optional[JianyingBuild]:
    candidates = []
    if not product_root.is_dir():
        return None
    for child in product_root.iterdir():
        if child.is_dir() and child.name in SUPPORTED_JIANYING_BUILDS:
            candidates.append(child)
    for candidate in sorted(candidates, key=lambda item: item.name, reverse=True):
        try:
            return detect_supported_build(candidate)
        except (FileNotFoundError, SafetyGateError):
            continue
    return None
