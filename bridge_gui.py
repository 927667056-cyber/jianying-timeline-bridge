from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
CLI = APP_DIR / "bridge_cli.py"
CONFIG = APP_DIR / "software_build" / "bridge_config.json"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _default_output_parent() -> Path:
    candidate = Path.home() / "Documents" / "剪映时间线双向桥"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


class BridgeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("剪映时间线双向桥 v0.1.1-alpha.1")
        self.geometry("920x690")
        self.minsize(820, 600)
        self.busy = False

        self.output_parent = tk.StringVar(value=str(_default_output_parent()))
        self.xml_path = tk.StringVar()
        self.srt_path = tk.StringVar()
        self.draft_name = tk.StringVar()
        self.deploy = tk.BooleanVar(value=False)
        self.reverse_draft = tk.StringVar()
        self.reverse_srt = tk.BooleanVar(value=True)

        self._build()
        self.after(250, self.run_doctor)

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(16, 14, 16, 8))
        header.pack(fill="x")
        ttk.Label(header, text="剪映时间线双向桥", font=("Microsoft YaHei UI", 18, "bold")).pack(
            side="left"
        )
        ttk.Label(
            header,
            text="严格门禁 · 永不覆盖 · 未知结构立即停止",
            foreground="#555555",
        ).pack(side="left", padx=16, pady=(7, 0))
        self.doctor_button = ttk.Button(header, text="环境自检", command=self.run_doctor)
        self.doctor_button.pack(side="right")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=16, pady=8)
        forward = ttk.Frame(notebook, padding=18)
        reverse = ttk.Frame(notebook, padding=18)
        recovery = ttk.Frame(notebook, padding=18)
        notebook.add(forward, text="时间线 → 剪映")
        notebook.add(reverse, text="剪映 → 时间线")
        notebook.add(recovery, text="事务恢复")
        self._build_forward(forward)
        self._build_reverse(reverse)
        self._build_recovery(recovery)

        log_frame = ttk.LabelFrame(self, text="运行结果", padding=8)
        log_frame.pack(fill="both", expand=False, padx=16, pady=(0, 16))
        self.log = tk.Text(log_frame, height=13, wrap="word", font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, browse) -> None:
        ttk.Label(parent, text=label, width=16).grid(row=row, column=0, sticky="w", pady=7)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=7)
        ttk.Button(parent, text="选择…", command=browse).grid(row=row, column=2, pady=7)

    def _build_forward(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        ttk.Label(
            frame,
            text="支持已验证的 FCP7 XML：单原片、30fps、连续 V1/A1、1×、无复杂特效。",
            foreground="#444444",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self._row(frame, 1, "达芬奇 XML", self.xml_path, self._choose_xml)
        self._row(frame, 2, "精准 SRT（可选）", self.srt_path, self._choose_srt)
        ttk.Label(frame, text="新草稿名称", width=16).grid(row=3, column=0, sticky="w", pady=7)
        ttk.Entry(frame, textvariable=self.draft_name).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=7
        )
        self._row(frame, 4, "记录保存位置", self.output_parent, self._choose_output_parent)
        ttk.Checkbutton(
            frame,
            text="验证后正式加入剪映草稿列表（不勾选则只生成并验证准备稿）",
            variable=self.deploy,
        ).grid(row=5, column=1, columnspan=2, sticky="w", padx=8, pady=12)
        self.forward_button = ttk.Button(frame, text="开始转换", command=self.run_forward)
        self.forward_button.grid(row=6, column=1, sticky="e", padx=8, pady=12)

    def _build_reverse(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        ttk.Label(
            frame,
            text="正式反导只接受本工具创建并带完整溯源记录的剪映草稿。",
            foreground="#444444",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self._row(frame, 1, "剪映草稿目录", self.reverse_draft, self._choose_reverse_draft)
        self._row(frame, 2, "导出保存位置", self.output_parent, self._choose_output_parent)
        ttk.Checkbutton(frame, text="同时导出 SRT", variable=self.reverse_srt).grid(
            row=3, column=1, columnspan=2, sticky="w", padx=8, pady=12
        )
        self.reverse_button = ttk.Button(frame, text="开始反导", command=self.run_reverse)
        self.reverse_button.grid(row=4, column=1, sticky="e", padx=8, pady=12)

    def _build_recovery(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        ttk.Label(
            frame,
            text="仅处理本工具留下的未完成部署日志；不会扫描或改动无关草稿。",
            foreground="#444444",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self._row(frame, 1, "报告保存位置", self.output_parent, self._choose_output_parent)
        self.recover_button = ttk.Button(frame, text="检查并恢复", command=self.run_recover)
        self.recover_button.grid(row=2, column=1, sticky="e", padx=8, pady=12)

    def _choose_xml(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("FCP7 XML", "*.xml"), ("所有文件", "*.*")])
        if value:
            self.xml_path.set(value)
            if not self.draft_name.get().strip():
                self.draft_name.set(f"{Path(value).stem}_剪映")

    def _choose_srt(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("SRT 字幕", "*.srt"), ("所有文件", "*.*")])
        if value:
            self.srt_path.set(value)

    def _choose_output_parent(self) -> None:
        value = filedialog.askdirectory(mustexist=True)
        if value:
            self.output_parent.set(value)

    def _choose_reverse_draft(self) -> None:
        value = filedialog.askdirectory(mustexist=True)
        if value:
            self.reverse_draft.set(value)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.doctor_button,
            self.forward_button,
            self.reverse_button,
            self.recover_button,
        ):
            button.configure(state=state)

    def _append(self, text: str) -> None:
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")

    def _run(self, label: str, args: list[str]) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._append(f"\n[{datetime.now():%H:%M:%S}] {label}")

        def worker() -> None:
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            completed = subprocess.run(
                [sys.executable, str(CLI), "--config", str(CONFIG), *args],
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                check=False,
            )
            self.after(0, lambda: self._finish(completed))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, completed: subprocess.CompletedProcess[str]) -> None:
        if completed.stdout.strip():
            self._append(completed.stdout)
        if completed.stderr.strip():
            self._append(completed.stderr)
        self._set_busy(False)
        if completed.returncode == 0:
            messagebox.showinfo("完成", "操作已通过全部门禁。详细路径见运行结果。")
        else:
            messagebox.showwarning("已安全停止", "操作未通过门禁，没有覆盖既有工程。请查看运行结果。")

    def run_doctor(self) -> None:
        self._run("环境自检", ["doctor"])

    def run_forward(self) -> None:
        xml = self.xml_path.get().strip()
        name = self.draft_name.get().strip()
        parent = Path(self.output_parent.get().strip())
        if not xml or not name or not parent.is_dir():
            messagebox.showwarning("信息不完整", "请选择 XML、填写新草稿名称，并选择记录保存位置。")
            return
        if self.deploy.get() and not messagebox.askyesno(
            "确认加入剪映",
            "该操作会新建剪映草稿并更新剪映草稿索引；不会覆盖同名工程。\n\n请确认剪映已经完全退出，是否继续？",
        ):
            return
        run_dir = parent / f"{name}_{_timestamp()}"
        args = [
            "to-jianying",
            "--xml",
            xml,
            "--draft-name",
            name,
            "--run-dir",
            str(run_dir),
        ]
        srt = self.srt_path.get().strip()
        if srt:
            args.extend(["--srt", srt])
        if self.deploy.get():
            args.append("--deploy")
        self._run("时间线 → 剪映", args)

    def run_reverse(self) -> None:
        draft = Path(self.reverse_draft.get().strip())
        parent = Path(self.output_parent.get().strip())
        if not draft.is_dir() or not parent.is_dir():
            messagebox.showwarning("信息不完整", "请选择剪映草稿目录和导出保存位置。")
            return
        output = parent / f"{draft.name}_反导_{_timestamp()}"
        args = ["from-jianying", "--draft", str(draft), "--output-bundle", str(output)]
        if not self.reverse_srt.get():
            args.append("--no-srt")
        self._run("剪映 → 时间线", args)

    def run_recover(self) -> None:
        parent = Path(self.output_parent.get().strip())
        if not parent.is_dir():
            messagebox.showwarning("信息不完整", "请选择报告保存位置。")
            return
        if not messagebox.askyesno(
            "确认恢复检查",
            "请确认剪映已经完全退出。恢复只处理本工具留下的事务日志，是否继续？",
        ):
            return
        report = parent / f"恢复报告_{_timestamp()}"
        self._run("事务恢复", ["recover", "--report-dir", str(report)])


if __name__ == "__main__":
    BridgeApp().mainloop()
