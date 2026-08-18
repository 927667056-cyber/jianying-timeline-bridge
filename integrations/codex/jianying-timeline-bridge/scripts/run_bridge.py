from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


DEFAULT_HOME = (
    Path.home() / "Documents" / "Codex" / "Tools" / "JianyingTimelineBridge" / "0.1.1"
)


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def find_home() -> Path:
    candidates = []
    configured = os.environ.get("JIANYING_TIMELINE_BRIDGE_HOME")
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).resolve().parents[4])
    candidates.append(DEFAULT_HOME)
    for candidate in candidates:
        if (candidate / "bridge_cli.py").is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "找不到剪映时间线双向桥软件。请安装软件，或设置 "
        "JIANYING_TIMELINE_BRIDGE_HOME 指向软件目录。"
    )


def main() -> int:
    configure_console()
    home = find_home()
    runtime = home / "runtime" / "python.exe"
    python = runtime if runtime.is_file() else Path(sys.executable)
    command = [str(python), str(home / "bridge_cli.py"), *sys.argv[1:]]
    return subprocess.run(command, cwd=str(home), check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
