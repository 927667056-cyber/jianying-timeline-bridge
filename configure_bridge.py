from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox


APP_DIR = Path(__file__).resolve().parent
EXAMPLE = APP_DIR / "software_build" / "bridge_config.example.json"
OUTPUT = APP_DIR / "software_build" / "bridge_config.json"

sys.path.insert(0, str(APP_DIR))

from bridge_safety import detect_supported_build


def choose_file(title: str, filename: str) -> Path:
    selected = filedialog.askopenfilename(title=title, filetypes=[(filename, filename), ("All files", "*")])
    if not selected:
        raise RuntimeError("配置已取消，未写入文件")
    return Path(selected).resolve()


def choose_directory(title: str) -> Path:
    selected = filedialog.askdirectory(title=title, mustexist=True)
    if not selected:
        raise RuntimeError("配置已取消，未写入文件")
    return Path(selected).resolve()


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    try:
        if OUTPUT.exists():
            raise FileExistsError(
                f"配置已经存在，工具不会覆盖：{OUTPUT}\n如需修改，请先人工备份并检查现有文件。"
            )
        config = json.loads(EXAMPLE.read_text(encoding="utf-8-sig"))
        install_dir = choose_directory("选择剪映 11.2.0.14339 安装目录")
        detect_supported_build(install_dir)
        ffmpeg = choose_file("选择 ffmpeg.exe", "ffmpeg.exe")
        ffprobe = choose_file("选择 ffprobe.exe", "ffprobe.exe")
        draft_root = choose_directory("选择 JianyingPro Drafts 草稿根目录")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("找不到 LOCALAPPDATA，无法定位剪映草稿索引")
        root_meta = (
            Path(local_app_data)
            / "JianyingPro"
            / "User Data"
            / "Projects"
            / "com.lveditor.draft"
            / "root_meta_info.json"
        )
        config["compatibility"]["jianying_install_dir"] = str(install_dir)
        config["tools"]["ffmpeg"] = str(ffmpeg)
        config["tools"]["ffprobe"] = str(ffprobe)
        config["draft_store"]["root"] = str(draft_root)
        config["draft_store"]["root_meta_info"] = str(root_meta)
        encoded = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT.open("xb") as handle:
            handle.write(encoded)
        messagebox.showinfo("剪映时间线双向桥", f"配置已创建：\n{OUTPUT}\n\n接下来请运行环境自检。")
        return 0
    except Exception as exc:
        messagebox.showerror("已安全停止", str(exc))
        return 2
    finally:
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
