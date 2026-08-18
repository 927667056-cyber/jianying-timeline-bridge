from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SKIP_DIRS = {".git", ".venv", "__pycache__", "runtime"}
SKIP_CONTENT_FILES = {"scripts/privacy_audit.py"}
PRIVATE_FILE_NAMES = {
    "deployment_manifest.json",
    "gate_report.json",
    "root_meta_info.json",
    "run_context.json",
    "timeline_bridge_provenance.json",
    "timeline_ir.json",
    "transaction_journal.json",
}
PRIVATE_SUFFIXES = {
    ".aaf",
    ".avi",
    ".docx",
    ".edl",
    ".fcpxml",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".srt",
    ".wav",
    ".xml",
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
USER_HOME_RE = re.compile(r"(?i)(?:[A-Z]:\\Users\\|/Users/|/home/)[^\s\"']+")
MACHINE_RE = re.compile(r"(?i)\bDESKTOP-[A-Z0-9]+\b")
IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
MAC_RE = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])")
ALLOWED_EMAIL_DOMAINS = {"example.com", "users.noreply.github.com"}


def iter_strings(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_strings(child)
    elif isinstance(value, str):
        yield value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--jianying-home", type=Path)
    parser.add_argument("--require-encrypted-inspection", action="store_true")
    parser.add_argument("--forbid", action="append", default=[])
    args = parser.parse_args()

    root = args.root.resolve()
    codec = None
    if args.jianying_home:
        sys.path.insert(0, str(root / "pyJianYingDraft-fork"))
        from pyJianYingDraft import DraftCryptoConfig, JianyingDraftCryptoCodec

        codec = JianyingDraftCryptoCodec(
            DraftCryptoConfig(
                jy_install_dir=args.jianying_home,
                isolated=True,
                validate_roundtrip=False,
                backup=False,
            )
        )

    hits: list[dict[str, str]] = []
    encrypted_files: list[str] = []
    inspected_encrypted_files: list[str] = []
    forbidden = [item.casefold() for item in args.forbid if item]

    def record(relative: str, kind: str, value: str) -> None:
        hits.append({"file": relative, "kind": kind, "value": value})

    def inspect_text(relative: str, text: str) -> None:
        folded = text.casefold()
        for original, token in zip(args.forbid, forbidden):
            if token in folded:
                record(relative, "forbidden_token", original)
        for email in EMAIL_RE.findall(text):
            domain = email.rsplit("@", 1)[-1].casefold()
            if domain not in ALLOWED_EMAIL_DOMAINS:
                record(relative, "email", email)
        for label, regex in (
            ("user_home", USER_HOME_RE),
            ("machine_name", MACHINE_RE),
            ("ip_address", IPV4_RE),
            ("mac_address", MAC_RE),
        ):
            for value in regex.findall(text):
                record(relative, label, value)

    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        lowered_name = path.name.casefold()
        if lowered_name in PRIVATE_FILE_NAMES or path.suffix.casefold() in PRIVATE_SUFFIXES:
            record(relative, "private_file", path.name)

        raw = path.read_bytes()
        parsed = None
        try:
            text = raw.decode("utf-8-sig")
            if relative not in SKIP_CONTENT_FILES:
                inspect_text(relative, text)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                pass
        except UnicodeDecodeError:
            pass

        if parsed is None and path.name in {"draft_content.json", "draft_meta_info.json"}:
            encrypted_files.append(relative)
            if codec is not None:
                parsed = codec.decode(raw)
                inspected_encrypted_files.append(relative)

        if parsed is not None and relative not in SKIP_CONTENT_FILES:
            for value in iter_strings(parsed):
                inspect_text(relative + " [json]", value)

    if args.require_encrypted_inspection and len(inspected_encrypted_files) != len(encrypted_files):
        record("[release]", "encrypted_uninspected", f"{len(encrypted_files) - len(inspected_encrypted_files)}")

    result = {
        "status": "passed" if not hits else "failed",
        "encrypted_files": encrypted_files,
        "inspected_encrypted_files": inspected_encrypted_files,
        "hits": hits,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
