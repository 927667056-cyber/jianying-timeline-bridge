from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import platform
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
FORK_DIR = SCRIPT_DIR / "pyJianYingDraft-fork"
if str(FORK_DIR) not in sys.path:
    sys.path.insert(0, str(FORK_DIR))

from bridge_safety import (  # noqa: E402
    BRIDGE_VERSION,
    SUPPORTED_JIANYING_BUILDS,
    SafetyGateError,
    detect_supported_build,
    ensure_new_output,
    guarded_child,
    jianying_is_running,
    require_jianying_closed,
    sha256_file,
    stable_read_bytes,
    validate_draft_name,
)


CONFIG_SCHEMA = "io.github.jianying-timeline-bridge.runtime-config"
CONFIG_SCHEMA_VERSION = 1
TEMPLATE_FINGERPRINT_ALGORITHM = "sha256-tree-path-size-filehash-v1"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "software_build" / "bridge_config.json"
STORE_LOCK_FILENAME = "._timeline_bridge_operation.lock"
READ_LOCK_PREFIX = "._timeline_bridge_read_"


class CliFailure(RuntimeError):
    """A user-facing, fail-closed command error."""


def _lock_path_for_store(config: Dict[str, Any]) -> Path:
    return config["_paths"]["draft_root"] / STORE_LOCK_FILENAME


def _lock_path_for_draft(draft_path: Path, config: Dict[str, Any]) -> Path:
    draft_path = draft_path.resolve()
    draft_root = config["_paths"]["draft_root"].resolve()
    if draft_path.parent == draft_root:
        return _lock_path_for_store(config)
    identity = hashlib.sha256(str(draft_path).casefold().encode("utf-8")).hexdigest()[:20]
    return draft_path.parent / f"{READ_LOCK_PREFIX}{identity}.lock"


def _lock_owner_summary(lock_path: Path) -> str:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return "锁文件存在，但内容无法读取"
    if not isinstance(payload, dict):
        return "锁文件存在，但内容格式无效"
    return (
        f"操作={payload.get('operation', '未知')}，"
        f"进程={payload.get('pid', '未知')}，"
        f"主机={payload.get('host', '未知')}，"
        f"开始时间戳={payload.get('created_at_us', '未知')}"
    )


def _lock_conflict_message(lock_path: Path) -> str:
    return (
        f"检测到时间线桥操作锁：{lock_path}（{_lock_owner_summary(lock_path)}）。"
        "为防止 root_meta_info.json 并发覆盖，本次已停止。"
        "如果另一个转换仍在运行，请等待它完成；如果确认是异常退出留下的 stale lock，"
        "请先确认锁中 PID 已不存在，保留一份锁文件作为记录，再手动删除该锁，"
        "随后先运行 recover，工具不会自动猜测或删除 stale lock。"
    )


@contextlib.contextmanager
def operation_lock(lock_path: Path, *, operation: str, resource: Path) -> Iterator[Path]:
    """Acquire a bridge-owned inter-process lock without replacing an existing file."""

    lock_path = lock_path.resolve()
    if not lock_path.parent.is_dir():
        raise FileNotFoundError(f"操作锁父目录不存在：{lock_path.parent}")
    token = uuid.uuid4().hex
    payload = {
        "schema": "io.github.jianying-timeline-bridge.operation-lock",
        "schema_version": 1,
        "bridge_version": BRIDGE_VERSION,
        "token": token,
        "operation": operation,
        "resource": str(resource.resolve()),
        "pid": os.getpid(),
        "host": platform.node(),
        "created_at_us": time.time_ns() // 1000,
        "command": [str(item) for item in sys.argv],
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise CliFailure(_lock_conflict_message(lock_path)) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        yield lock_path
    finally:
        if lock_path.exists():
            try:
                current = json.loads(lock_path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                raise CliFailure(
                    f"操作锁内容在运行期间损坏，已保留现场且未自动删除：{lock_path}"
                ) from exc
            if not isinstance(current, dict) or current.get("token") != token:
                raise CliFailure(
                    f"操作锁在运行期间被其他进程替换，已保留现场且未自动删除：{lock_path}"
                )
            lock_path.unlink()


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"找不到配置或报告文件：{path}") from exc
    except PermissionError as exc:
        raise PermissionError(f"没有权限读取文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise CliFailure(f"JSON 文件格式无效：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise CliFailure(f"JSON 顶层必须是对象：{path}")
    return payload


def _required_object(payload: Dict[str, Any], key: str, label: str) -> Dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise CliFailure(f"配置缺少对象：{label}.{key}")
    return value


def _required_string(payload: Dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CliFailure(f"配置缺少非空字符串：{label}.{key}")
    return value


def _resolve_configured_path(raw: str, config_path: Path) -> Path:
    expanded = os.path.expandvars(raw)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve()


def load_config(config_path: Path) -> Dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    payload = _read_json(config_path)
    if payload.get("schema") != CONFIG_SCHEMA:
        raise CliFailure("配置文件 schema 不受支持；已停止，未写入任何草稿")
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise CliFailure("配置文件版本不受支持；已停止，未写入任何草稿")

    compatibility = _required_object(payload, "compatibility", "config")
    tools = _required_object(payload, "tools", "config")
    store = _required_object(payload, "draft_store", "config")
    template = _required_object(payload, "clean_template", "config")

    expected_build = _required_string(compatibility, "jianying_build", "compatibility")
    expected_dll = _required_string(
        compatibility, "videoeditor_dll_sha256", "compatibility"
    ).lower()
    calibrated_hash = SUPPORTED_JIANYING_BUILDS.get(expected_build)
    if calibrated_hash is None or calibrated_hash.lower() != expected_dll:
        raise CliFailure("配置试图使用未校准的剪映版本或 DLL 指纹；已停止")
    if compatibility.get("fps") != 30:
        raise CliFailure("当前正式路线只接受 30fps 配置")
    if compatibility.get("draft_version") != 360000:
        raise CliFailure("当前正式路线只接受 draft version 360000")
    if compatibility.get("new_version") != "181.0.0":
        raise CliFailure("当前正式路线只接受 draft new_version 181.0.0")

    fingerprint_algorithm = _required_string(
        template, "fingerprint_algorithm", "clean_template"
    )
    if fingerprint_algorithm != TEMPLATE_FINGERPRINT_ALGORITHM:
        raise CliFailure("干净模板使用了未知的指纹算法；已停止")
    template_sha256 = _required_string(template, "sha256", "clean_template").lower()
    if len(template_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in template_sha256):
        raise CliFailure("干净模板 SHA-256 配置无效")
    file_count = template.get("file_count")
    if type(file_count) is not int or file_count <= 0:
        raise CliFailure("干净模板 file_count 配置无效")

    resolved = {
        **payload,
        "_config_path": config_path,
        "_paths": {
            "jianying_install_dir": _resolve_configured_path(
                _required_string(compatibility, "jianying_install_dir", "compatibility"),
                config_path,
            ),
            "ffmpeg": _resolve_configured_path(
                _required_string(tools, "ffmpeg", "tools"), config_path
            ),
            "ffprobe": _resolve_configured_path(
                _required_string(tools, "ffprobe", "tools"), config_path
            ),
            "draft_root": _resolve_configured_path(
                _required_string(store, "root", "draft_store"), config_path
            ),
            "root_meta_info": _resolve_configured_path(
                _required_string(store, "root_meta_info", "draft_store"), config_path
            ),
            "clean_template": _resolve_configured_path(
                _required_string(template, "path", "clean_template"), config_path
            ),
        },
    }
    return resolved


def compute_template_fingerprint(template_dir: Path) -> Dict[str, Any]:
    if not template_dir.is_dir():
        raise FileNotFoundError(f"配置的干净模板目录不存在：{template_dir}")
    files = []
    folded_names: Dict[str, str] = {}
    for candidate in template_dir.rglob("*"):
        if candidate.is_symlink():
            raise CliFailure(f"干净模板不能包含符号链接：{candidate}")
        if candidate.is_file():
            relative = candidate.relative_to(template_dir).as_posix()
            folded = relative.casefold()
            if folded in folded_names:
                raise CliFailure(
                    f"干净模板含有仅大小写不同的重复路径：{folded_names[folded]} / {relative}"
                )
            folded_names[folded] = relative
            files.append((relative, candidate))
    files.sort(key=lambda item: item[0].casefold())
    if not files:
        raise CliFailure("配置的干净模板是空目录")

    digest = hashlib.sha256()
    for relative, path in files:
        stat = path.stat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\n")
    return {
        "algorithm": TEMPLATE_FINGERPRINT_ALGORITHM,
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for _, path in files),
    }


def require_clean_template(config: Dict[str, Any]) -> Tuple[Path, Dict[str, Any]]:
    template_config = config["clean_template"]
    template_path = config["_paths"]["clean_template"]
    actual = compute_template_fingerprint(template_path)
    if actual["file_count"] != template_config["file_count"]:
        raise CliFailure(
            "干净模板文件数量与锁定配置不一致；模板可能被修改，已停止"
        )
    if actual["sha256"].lower() != template_config["sha256"].lower():
        raise CliFailure("干净模板整目录指纹不一致；模板可能被修改，已停止")
    return template_path, actual


def _import_bridge_modules() -> Tuple[Any, Any, Any]:
    try:
        dependency = importlib.import_module("pyJianYingDraft")
        forward = importlib.import_module("jianying_xml_adapter")
        reverse = importlib.import_module("jianying_to_fcp7")
    except Exception as exc:
        raise CliFailure(f"Python 依赖加载失败：{type(exc).__name__}: {exc}") from exc
    required = {
        "jianying_xml_adapter": (
            "analyze",
            "prepare_draft",
            "deploy",
            "verify_draft",
            "recover_deployments",
        ),
        "jianying_to_fcp7": ("reverse_bundle",),
        "pyJianYingDraft": ("JianyingDraftCryptoCodec", "DraftCryptoConfig"),
    }
    modules = {
        "jianying_xml_adapter": forward,
        "jianying_to_fcp7": reverse,
        "pyJianYingDraft": dependency,
    }
    for module_name, names in required.items():
        for name in names:
            if not callable(getattr(modules[module_name], name, None)):
                raise CliFailure(f"Python 依赖缺少接口：{module_name}.{name}")
    return forward, reverse, dependency


def _check_python_runtime() -> Dict[str, Any]:
    if os.name != "nt":
        raise CliFailure("当前工具只支持 Windows")
    if sys.version_info < (3, 10):
        raise CliFailure("Python 版本过低；需要 Python 3.10 或更高版本")
    bits = struct.calcsize("P") * 8
    if bits != 64:
        raise CliFailure("需要 64 位 Python")
    forward, reverse, dependency = _import_bridge_modules()
    return {
        "python": platform.python_version(),
        "bits": bits,
        "executable": str(Path(sys.executable).resolve()),
        "dependencies": {
            "pyJianYingDraft": str(Path(dependency.__file__).resolve()),
            "jianying_xml_adapter": str(Path(forward.__file__).resolve()),
            "jianying_to_fcp7": str(Path(reverse.__file__).resolve()),
        },
    }


def _check_build(config: Dict[str, Any]) -> Dict[str, Any]:
    expected = config["compatibility"]
    install_dir = config["_paths"]["jianying_install_dir"]
    executable = install_dir / "JianyingPro.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"找不到剪映主程序：{executable}")
    detected = detect_supported_build(install_dir)
    if detected.version != expected["jianying_build"]:
        raise CliFailure("实际剪映 build 与配置不一致")
    if detected.dll_sha256.lower() != expected["videoeditor_dll_sha256"].lower():
        raise CliFailure("实际 videoeditor.dll 指纹与配置不一致")
    return {
        "build": detected.version,
        "install_dir": str(detected.install_dir),
        "dll": str(detected.dll_path),
        "dll_sha256": detected.dll_sha256,
    }


def _check_tool(path: Path, label: str) -> Dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到 {label}：{path}")
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliFailure(f"{label} 版本检查超时：{path}") from exc
    if completed.returncode != 0:
        raise CliFailure(f"{label} 无法运行（返回码 {completed.returncode}）：{path}")
    lines = [line.strip() for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()]
    return {"path": str(path), "version": lines[0] if lines else "可运行"}


def _check_template_structure(config: Dict[str, Any]) -> Dict[str, Any]:
    template_path, fingerprint = require_clean_template(config)
    _, _, dependency = _import_bridge_modules()
    build = _check_build(config)
    required = (
        template_path / "draft_content.json",
        template_path / "draft_meta_info.json",
        template_path / "Timelines" / "project.json",
    )
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f"干净模板缺少必要文件：{path}")
    codec = dependency.JianyingDraftCryptoCodec(
        dependency.DraftCryptoConfig(
            jy_install_dir=build["install_dir"],
            isolated=True,
            validate_roundtrip=True,
            backup=False,
        )
    )
    content = codec.decode((template_path / "draft_content.json").read_bytes())
    meta = codec.decode((template_path / "draft_meta_info.json").read_bytes())
    project = _read_json(template_path / "Timelines" / "project.json")
    timeline_id = project.get("main_timeline_id")
    if not isinstance(timeline_id, str) or not timeline_id:
        raise CliFailure("干净模板缺少 main_timeline_id")
    nested_content = template_path / "Timelines" / timeline_id / "draft_content.json"
    if not nested_content.is_file():
        raise FileNotFoundError(f"干净模板缺少时间线内容：{nested_content}")
    if codec.decode(nested_content.read_bytes()) != content:
        raise CliFailure("干净模板根内容与时间线内容不一致")
    expected = config["compatibility"]
    if content.get("version") != expected["draft_version"]:
        raise CliFailure("干净模板 draft version 与配置不一致")
    if content.get("new_version") != expected["new_version"]:
        raise CliFailure("干净模板 new_version 与配置不一致")
    if content.get("platform", {}).get("app_version") != "11.2.0":
        raise CliFailure("干净模板不是剪映 11.2.0 草稿")
    if content.get("id") != timeline_id:
        raise CliFailure("干净模板 timeline id 不一致")
    if not isinstance(meta.get("draft_id"), str) or not meta.get("draft_id"):
        raise CliFailure("干净模板 draft_id 无效")
    return {
        "path": str(template_path),
        "fingerprint": fingerprint,
        "draft_version": content.get("version"),
        "new_version": content.get("new_version"),
        "app_version": content.get("platform", {}).get("app_version"),
        "timeline_id": timeline_id,
    }


def _check_draft_root(config: Dict[str, Any]) -> Dict[str, Any]:
    draft_root = config["_paths"]["draft_root"]
    if not draft_root.is_dir():
        raise FileNotFoundError(f"剪映草稿根目录不存在：{draft_root}")
    return {
        "path": str(draft_root),
        "readable": os.access(draft_root, os.R_OK),
        "writable": os.access(draft_root, os.W_OK),
    }


def _check_root_meta(config: Dict[str, Any]) -> Dict[str, Any]:
    root_meta = config["_paths"]["root_meta_info"]
    draft_root = config["_paths"]["draft_root"]
    try:
        is_file = root_meta.is_file()
    except PermissionError as exc:
        raise PermissionError(f"没有权限读取剪映草稿索引：{root_meta}") from exc
    if not is_file:
        raise FileNotFoundError(f"找不到剪映草稿索引 root_meta_info.json：{root_meta}")
    try:
        raw, digest = stable_read_bytes(root_meta)
    except PermissionError as exc:
        raise PermissionError(f"没有权限读取剪映草稿索引：{root_meta}") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliFailure(f"剪映草稿索引不是有效 JSON：{root_meta}") from exc
    if not isinstance(payload, dict):
        raise CliFailure("剪映草稿索引顶层不是对象")
    entries = payload.get("all_draft_store")
    if not isinstance(entries, list):
        raise CliFailure("剪映草稿索引 all_draft_store 不是数组")
    root_norm = str(draft_root.resolve()).replace("\\", "/").rstrip("/").casefold()
    matching = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise CliFailure("剪映草稿索引含有非对象条目")
        entry_root = str(entry.get("draft_root_path", "")).replace("\\", "/").rstrip("/").casefold()
        if entry_root == root_norm:
            matching += 1
    return {
        "path": str(root_meta),
        "sha256": digest,
        "entry_count": len(entries),
        "entries_for_configured_root": matching,
    }


def _check_jianying_closed() -> Dict[str, Any]:
    running = jianying_is_running()
    if running:
        raise SafetyGateError("剪映仍在运行；请完全退出剪映后重试")
    return {"running": False}


def _check_operation_lock(config: Dict[str, Any]) -> Dict[str, Any]:
    lock_path = _lock_path_for_store(config)
    if lock_path.exists():
        raise CliFailure(_lock_conflict_message(lock_path))
    return {"store_lock": str(lock_path), "present": False}


def _record_check(
    checks: Dict[str, Dict[str, Any]], name: str, checker: Callable[[], Dict[str, Any]]
) -> None:
    try:
        details = checker()
        checks[name] = {"passed": True, **details}
    except Exception as exc:
        checks[name] = {
            "passed": False,
            "error": str(exc) or type(exc).__name__,
            "error_type": type(exc).__name__,
        }


def doctor(config: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    checks: Dict[str, Dict[str, Any]] = {}
    _record_check(checks, "python_dependencies", _check_python_runtime)
    _record_check(checks, "jianying_exact_build_and_dll", lambda: _check_build(config))
    _record_check(
        checks, "ffmpeg", lambda: _check_tool(config["_paths"]["ffmpeg"], "ffmpeg")
    )
    _record_check(
        checks, "ffprobe", lambda: _check_tool(config["_paths"]["ffprobe"], "ffprobe")
    )
    _record_check(checks, "clean_template", lambda: _check_template_structure(config))
    _record_check(checks, "jianying_closed", _check_jianying_closed)
    _record_check(checks, "draft_root", lambda: _check_draft_root(config))
    _record_check(checks, "root_meta_info", lambda: _check_root_meta(config))
    _record_check(checks, "operation_lock", lambda: _check_operation_lock(config))
    passed = all(item["passed"] for item in checks.values())
    result = {
        "status": "passed" if passed else "failed",
        "bridge_version": BRIDGE_VERSION,
        "config": str(config["_config_path"]),
        "checks": checks,
    }
    return (0 if passed else 2), result


def _require_forward_environment(config: Dict[str, Any]) -> Tuple[Any, Path]:
    _check_python_runtime()
    _check_build(config)
    _check_tool(config["_paths"]["ffmpeg"], "ffmpeg")
    _check_tool(config["_paths"]["ffprobe"], "ffprobe")
    require_jianying_closed()
    _check_draft_root(config)
    _check_root_meta(config)
    template_path, _ = require_clean_template(config)
    _check_template_structure(config)
    forward, _, _ = _import_bridge_modules()
    return forward, template_path


def _require_reverse_environment(config: Dict[str, Any]) -> Any:
    _check_python_runtime()
    _check_build(config)
    _check_tool(config["_paths"]["ffprobe"], "ffprobe")
    require_jianying_closed()
    _, reverse, _ = _import_bridge_modules()
    return reverse


def _require_recovery_environment(config: Dict[str, Any]) -> Any:
    _check_python_runtime()
    require_jianying_closed()
    _check_draft_root(config)
    _check_root_meta(config)
    forward, _, _ = _import_bridge_modules()
    return forward


def command_to_jianying(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    xml_path = args.xml.resolve()
    if not xml_path.is_file():
        raise FileNotFoundError(f"找不到达芬奇 XML：{xml_path}")
    srt_path = args.srt.resolve() if args.srt is not None else None
    if srt_path is not None and not srt_path.is_file():
        raise FileNotFoundError(f"找不到字幕 SRT：{srt_path}")
    draft_name = validate_draft_name(args.draft_name)
    run_dir = args.run_dir.resolve()
    ensure_new_output(run_dir, kind="本次转换记录目录")

    draft_root = config["_paths"]["draft_root"]
    final_target = guarded_child(draft_root, draft_name)
    if final_target.exists():
        raise FileExistsError(f"同名剪映草稿已经存在，工具不会覆盖：{final_target}")

    forward, template_path = _require_forward_environment(config)
    lock_context = (
        operation_lock(
            _lock_path_for_store(config),
            operation="to-jianying-deploy-and-formal-verify",
            resource=draft_root,
        )
        if args.deploy
        else contextlib.nullcontext(None)
    )
    with lock_context:
        if args.deploy:
            require_jianying_closed()
            _check_root_meta(config)
            if final_target.exists():
                raise FileExistsError(f"同名剪映草稿已经存在，工具不会覆盖：{final_target}")
        run_dir.mkdir()
        report_dir = run_dir / "01_analyze_and_prepare"
        prepared_root = run_dir / "prepared"
        prepared_verify_dir = run_dir / "02_verify_prepared"
        deploy_report_dir = run_dir / "03_deploy"
        final_verify_dir = run_dir / "04_verify_deployed"

        ir, media, gate_report = forward.analyze(
            xml_path,
            config["_paths"]["ffprobe"],
            report_dir,
            srt_path,
        )
        prepared = forward.prepare_draft(
            ir,
            media,
            report_dir,
            prepared_root,
            draft_root,
            draft_name,
            template_path,
            config["_paths"]["jianying_install_dir"],
            config["_paths"]["ffmpeg"],
            srt_path,
        )
        manifest_path = report_dir / "prepared_manifest.json"
        prepared_verification = forward.verify_draft(
            prepared,
            manifest_path,
            config["_paths"]["jianying_install_dir"],
            None,
            prepared_verify_dir,
        )

        deployed: Optional[Path] = None
        final_verification: Optional[Dict[str, Any]] = None
        if args.deploy:
            deployed = forward.deploy(
                prepared,
                manifest_path,
                config["_paths"]["root_meta_info"],
                deploy_report_dir,
            )
            final_verification = forward.verify_draft(
                deployed,
                manifest_path,
                config["_paths"]["jianying_install_dir"],
                config["_paths"]["root_meta_info"],
                final_verify_dir,
            )

    return {
        "status": "deployed_and_verified" if deployed is not None else "prepared_and_verified",
        "run_dir": str(run_dir),
        "draft_name": draft_name,
        "source_xml": str(xml_path),
        "source_srt": str(srt_path) if srt_path is not None else None,
        "configured_clean_template": str(template_path),
        "analyze_status": gate_report.get("status"),
        "prepared": str(prepared),
        "prepared_verification": prepared_verification,
        "deployed": str(deployed) if deployed is not None else None,
        "final_verification": final_verification,
        "manifest": str(manifest_path),
    }


def command_from_jianying(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    draft_path = args.draft.resolve()
    if not draft_path.is_dir():
        raise FileNotFoundError(f"找不到剪映草稿目录：{draft_path}")
    output_bundle = args.output_bundle.resolve()
    ensure_new_output(output_bundle, kind="反向导出目录")
    reverse = _require_reverse_environment(config)
    with operation_lock(
        _lock_path_for_draft(draft_path, config),
        operation="from-jianying-stable-read",
        resource=draft_path,
    ):
        require_jianying_closed()
        return reverse.reverse_bundle(
            draft_path,
            output_bundle,
            include_srt=not args.no_srt,
            jianying_install_dir=config["_paths"]["jianying_install_dir"],
            ffprobe_path=config["_paths"]["ffprobe"],
            fps=config["compatibility"]["fps"],
            sequence_name=args.sequence_name,
        )


def command_recover(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    report_dir = args.report_dir.resolve()
    ensure_new_output(report_dir, kind="恢复报告目录")
    forward = _require_recovery_environment(config)
    with operation_lock(
        _lock_path_for_store(config),
        operation="recover-deployments",
        resource=config["_paths"]["draft_root"],
    ):
        require_jianying_closed()
        _check_root_meta(config)
        return forward.recover_deployments(
            config["_paths"]["draft_root"],
            config["_paths"]["root_meta_info"],
            report_dir,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="达芬奇 FCP7 XML 与剪映 11.2 双向时间线桥（严格门禁、永不覆盖）"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"运行配置（默认：{DEFAULT_CONFIG_PATH}）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="只读检查剪映、工具、模板和草稿索引")

    forward_parser = subparsers.add_parser(
        "to-jianying", help="达芬奇 XML 生成剪映草稿；默认只准备，不安装到剪映"
    )
    forward_parser.add_argument("--xml", required=True, type=Path, help="FCP7/xmeml v5 XML")
    forward_parser.add_argument("--srt", type=Path, help="可选的精准单行 SRT")
    forward_parser.add_argument("--draft-name", required=True, help="新的剪映草稿名称")
    forward_parser.add_argument(
        "--run-dir", required=True, type=Path, help="必须尚不存在的本次转换记录目录"
    )
    forward_parser.add_argument(
        "--deploy", action="store_true", help="验证准备稿后，安装到剪映并再次正式验证"
    )

    reverse_parser = subparsers.add_parser(
        "from-jianying", help="有受管 provenance 的剪映草稿反导为达芬奇 XML"
    )
    reverse_parser.add_argument("--draft", required=True, type=Path, help="剪映草稿目录")
    reverse_parser.add_argument(
        "--output-bundle", required=True, type=Path, help="必须尚不存在的反向导出目录"
    )
    reverse_parser.add_argument("--no-srt", action="store_true", help="不导出 SRT")
    reverse_parser.add_argument("--sequence-name", help="可选的达芬奇序列名称")

    recover_parser = subparsers.add_parser(
        "recover", help="只恢复本工具未完成的部署事务，不触碰其他草稿"
    )
    recover_parser.add_argument(
        "--report-dir", required=True, type=Path, help="必须尚不存在的恢复报告目录"
    )
    return parser


def _print_json(payload: Dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            return_code, result = doctor(config)
        elif args.command == "to-jianying":
            result = command_to_jianying(args, config)
            return_code = 0
        elif args.command == "from-jianying":
            result = command_from_jianying(args, config)
            return_code = 0
        elif args.command == "recover":
            result = command_recover(args, config)
            return_code = 0
        else:
            raise AssertionError(args.command)
        _print_json(result)
        return return_code
    except Exception as exc:
        _print_json(
            {
                "status": "failed",
                "command": getattr(args, "command", None),
                "error": str(exc) or type(exc).__name__,
                "error_type": type(exc).__name__,
                "safety": "已停止；不会覆盖已有输出或同名剪映草稿",
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
