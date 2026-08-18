from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "package_manifest.json"
SKIP_NAMES = {".git", "__pycache__", ".venv", "runtime"}
SKIP_PATHS = {"software_build/bridge_config.json", "package_manifest.json"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    files = []
    for path in sorted(ROOT.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or any(part in SKIP_NAMES for part in path.parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in SKIP_PATHS or relative.endswith((".pyc", ".pyo")):
            continue
        files.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": sha256(path)})
    payload = {
        "schema": "io.github.jianying-timeline-bridge.package-manifest",
        "schema_version": 1,
        "bridge_version": "0.1.1-alpha.1",
        "release_tier": "narrow-verified-alpha",
        "supported_jianying_build": "11.2.0.14339",
        "supported_videoeditor_dll_sha256": "654371a6dca840f53ae786df43fc345365e54d2189d5572ce7c4658aab23f540",
        "public_smoke_tests": "13/13",
        "validation_summary": {
            "independent_long_form_timelines": 2,
            "paired_av_segments": "hundreds",
            "captions": "hundreds",
            "timeline_frames": "more_than_30000",
            "duration_policies": ["exact", "one_unused_trailing_frame"],
        },
        "file_count": len(files),
        "files": files,
    }
    OUTPUT.write_bytes((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
