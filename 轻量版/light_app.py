# -*- coding: utf-8 -*-
"""抖音数据提取工具（轻量版）。

轻量版只保留一条明确的数据链路：输入抖音链接，提取标题、标签、点赞、
收藏和评论，增量保存到 ``提取记录.xlsx``。视频、图集、封面、文案、
账号监控、刷新、删除、导入和其它完整版本功能均不在此界面中。
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import random
import re
import sys
import threading
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
import tkinter as tk


# 源码模式从项目根目录复用经过验证的只读页面解析引擎；PyInstaller 会把
# extractor.py 和 tasking.py 一起收进轻量版发布目录，不会收录完整 app.py。
SOURCE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SOURCE_DIR.parent
if not getattr(sys, "frozen", False) and str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import extractor  # noqa: E402
import input_parser  # noqa: E402
import light_exporter  # noqa: E402
from tasking import TaskCancelled, ensure_not_cancelled, interruptible_wait  # noqa: E402


APP_VERSION = "1.0.1"
APP_TITLE = "抖音数据提取工具（轻量版）"
LOG_NAME = "提取日志.log"
DIVIDER_RE = input_parser.DIVIDER_RE
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR


def _writable_state_dir() -> Path:
    """优先使用发布目录的 data；目录不可写时回退到本机状态目录。"""
    preferred = APP_DIR / "data"
    candidates = [preferred]
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / APP_TITLE)

    last_error: OSError | None = None
    for candidate in candidates:
        probe = candidate / ".write_test"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError as exc:
            last_error = exc
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    raise OSError(f"程序状态目录不可写：{last_error}")


STATE_DIR = _writable_state_dir()
CONFIG_FILE = STATE_DIR / "config.json"
INPUT_CACHE_FILE = STATE_DIR / "input_cache.txt"
BROWSER_PROFILE_DIR = STATE_DIR / "browser_profile"


def load_config() -> dict:
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8").lstrip("\ufeff"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_config(updates: dict) -> None:
    try:
        config = load_config()
        config.update(updates)
        CONFIG_FILE.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def load_input_cache() -> str:
    try:
        return INPUT_CACHE_FILE.read_text(encoding="utf-8").lstrip("\ufeff").rstrip("\n")
    except OSError:
        return ""


def default_output_dir() -> str:
    """使用独立的轻量版数据目录，避免碰到完整版本的输出。"""
    desktop = Path.home() / "Desktop"
    if desktop.exists():
        return str(desktop / "抖音信息提取工具-轻量版数据")
    return str(APP_DIR / "output")


def setup_logger(log_path: Path) -> logging.Logger:
    """把轻量版和页面解析引擎的日志写入同一个轮转日志。"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(handler)
    return logging.getLogger("douyin_light")


def _as_int(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def extract_collects(item: dict) -> int:
    """读取抖音作品统计中的收藏数，兼容常见字段命名。"""
    statistics = item.get("statistics") or {}
    for key in (
        "collect_count",
        "collects_count",
        "favorite_count",
        "favorites_count",
    ):
        if key in statistics and statistics.get(key) is not None:
            return _as_int(statistics.get(key))
    return 0


def extract_author_id(item: dict) -> str:
    """提取公开作者标识：优先稳定的 sec_uid，缺失时回退到 uid。"""
    author = item.get("author") or {}
    for key in ("sec_uid", "sec_user_id", "uid_str", "uid"):
        value = author.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def build_light_record(fetched: extractor.FetchedRecord, raw_input: str) -> dict:
    fields = fetched.fields
    return {
        "raw_input": raw_input,
        "title": fields.get("title") or "无",
        "tags": fields.get("tags") or "无",
        "likes": _as_int(fields.get("likes")),
        "collects": extract_collects(fetched.item),
        "comments": _as_int(fields.get("comments")),
        "status": "正常",
        "aweme_id": fetched.aweme_id,
        "author_id": extract_author_id(fetched.item),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def fetch_with_retry(
    logger: logging.Logger,
    seq: int,
    link: str,
    cancel_event: threading.Event,
    access_context: extractor.AccessContext,
) -> tuple[extractor.FetchedRecord | None, str | None]:
    """对可恢复的获取失败有限重试；验证失败交给批次暂停逻辑。"""
    last_error = "获取失败"
    for attempt in (1, 2):
        ensure_not_cancelled(cancel_event)
        try:
            return access_context.fetch_record(link), None
        except TaskCancelled:
            raise
        except extractor.BrowserVerificationError:
            raise
        except extractor.InvalidLinkError:
            return None, "链接无效"
        except extractor.TargetUnavailableError:
            return None, "目标作品已失效（浏览器自动跳转到其他作品）"
        except extractor.LoginRequiredError as exc:
            last_error = f"目标作品暂不可用（{exc}）"
        except extractor.PageStructureError as exc:
            last_error = f"获取失败（页面结构可能已变化：{exc}）"
        except extractor.CaptchaChallengeError as exc:
            last_error = f"风控或验证异常（{exc}）"
        except extractor.WafBlockedError as exc:
            last_error = f"风控或网络异常（{exc}）"
        except Exception as exc:
            last_error = f"获取失败（{exc}）"

        if attempt == 1:
            logger.warning("顺序 %d：%s，有限重试一次", seq, last_error)
            interruptible_wait(2 + random.random() * 2, cancel_event)
    return None, last_error


class LightDouyinApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x700")
        self.root.minsize(1000, 560)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.message_queue: queue.Queue[tuple[str, object, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.running = False
        self.close_requested = False
        self._renumber_after_id = None
        self._updating_input = False
        self.failed_jobs: list[tuple[int | None, str]] = []
        self.success_count = 0
        self.failure_count = 0
        self.unchecked_count = 0
        self.paused_message = ""
        self.logger = logging.getLogger("douyin_light")
        self.visible_records: dict[str, dict] = {}

        config = load_config()
        self.output_dir = Path(config.get("output_dir") or default_output_dir())
        self._build_ui()

        cached = load_input_cache()
        if cached.strip() and cached.strip() != "1.":
            self.input_text.delete("1.0", "end")
            self.input_text.insert("1.0", cached)
        self._renumber_input()
        self.input_text.mark_set("insert", "end-1c")
        self.input_text.see("insert")
        self.load_existing_records()

    def _build_ui(self) -> None:
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="选择输出目录…", command=self.browse_output)
        file_menu.add_command(label="打开输出目录", command=self.open_output)
        file_menu.add_command(label="打开日志", command=self.open_log)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menu_bar.add_cascade(label="文件", menu=file_menu)
        self.root.config(menu=menu_bar)

        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="轻量版：只提取标题、标签、点赞、收藏、评论，不下载视频/图片/封面/文案",
        ).pack(side="left", anchor="w")
        ttk.Label(header, text=f"版本 {APP_VERSION}").pack(side="right")

        input_header = ttk.Frame(main)
        input_header.pack(fill="x", pady=(12, 4))
        ttk.Label(
            input_header,
            text="粘贴抖音作品链接或分享文案（每条自动编号）:",
        ).pack(side="left")
        self.clear_button = ttk.Button(
            input_header, text="清空输入", command=self.clear_input
        )
        self.clear_button.pack(side="right")

        input_frame = ttk.Frame(main)
        input_frame.pack(fill="x")
        self.input_text = tk.Text(
            input_frame,
            height=9,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            undo=True,
        )
        input_scroll = ttk.Scrollbar(
            input_frame, orient="vertical", command=self.input_text.yview
        )
        self.input_text.configure(yscrollcommand=input_scroll.set)
        self.input_text.pack(side="left", fill="both", expand=True)
        input_scroll.pack(side="right", fill="y")
        self.input_text.tag_configure("seq", foreground="#777777", background="#f3f3f3")
        self.input_text.tag_configure("divider", foreground="#aaaaaa")
        self.input_text.insert("1.0", "1.")
        self.input_text.bind("<KeyRelease>", self._schedule_renumber)
        # 只在用户明确触发粘贴事件时读取剪贴板，不做后台轮询。
        self.input_text.bind("<<Paste>>", self._on_input_paste)

        output_frame = ttk.Frame(main)
        output_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(output_frame, text="输出目录:").pack(side="left")
        self.output_var = tk.StringVar(value=str(self.output_dir))
        self.output_entry = ttk.Entry(output_frame, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.output_browse_button = ttk.Button(
            output_frame, text="浏览…", command=self.browse_output
        )
        self.output_browse_button.pack(side="right")

        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(10, 8))
        self.start_button = ttk.Button(
            controls, text="开始提取", command=self.start
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            controls, text="停止任务", command=self.stop_task, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(controls, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 8))
        self.progress_label = ttk.Label(controls, text="")
        self.progress_label.pack(side="right")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var).pack(fill="x", pady=(0, 6))

        result_frame = ttk.LabelFrame(main, text="提取结果（同时保存到提取记录.xlsx）", padding=6)
        result_frame.pack(fill="both", expand=True)
        columns = (
            "seq",
            "aweme_id",
            "author_id",
            "title",
            "tags",
            "likes",
            "collects",
            "comments",
            "status",
        )
        tree_frame = ttk.Frame(result_frame)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        headings = {
            "seq": ("序号", 58),
            "aweme_id": ("作品ID", 155),
            "author_id": ("作者ID", 250),
            "title": ("标题", 250),
            "tags": ("标签", 190),
            "likes": ("点赞", 90),
            "collects": ("收藏", 90),
            "comments": ("评论", 90),
            "status": ("状态", 200),
        }
        for column in columns:
            title, width = headings[column]
            self.tree.heading(column, text=title)
            self.tree.column(
                column,
                width=width,
                minwidth=45,
                anchor="w" if column in {"title", "tags", "status"} else "center",
            )
        self.tree.pack(side="left", fill="both", expand=True)
        result_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview
        )
        result_xscroll = ttk.Scrollbar(
            result_frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=result_scroll.set,
            xscrollcommand=result_xscroll.set,
        )
        result_scroll.pack(side="right", fill="y")
        result_xscroll.pack(side="bottom", fill="x")

    def _post(self, kind: str, payload=None, extra: str = "") -> None:
        self.message_queue.put((kind, payload, extra))

    def _save_input_cache(self) -> None:
        try:
            content = self.input_text.get("1.0", "end").rstrip("\n")
            INPUT_CACHE_FILE.write_text(content + "\n", encoding="utf-8")
        except OSError:
            pass

    def _schedule_renumber(self, _event=None) -> None:
        if self._renumber_after_id is not None:
            try:
                self.root.after_cancel(self._renumber_after_id)
            except tk.TclError:
                pass
        self._renumber_after_id = self.root.after_idle(self._renumber_input)

    def _style_input(self) -> None:
        self.input_text.tag_remove("seq", "1.0", "end")
        self.input_text.tag_remove("divider", "1.0", "end")
        for line_number, line in enumerate(
            self.input_text.get("1.0", "end").splitlines(), 1
        ):
            stripped = line.strip()
            if DIVIDER_RE.match(stripped):
                self.input_text.tag_add("divider", f"{line_number}.0", f"{line_number}.end")
            elif re.match(r"^\d+[.]", stripped):
                prefix = re.match(r"^\d+[.]", stripped).group(0)
                self.input_text.tag_add(
                    "seq",
                    f"{line_number}.0",
                    f"{line_number}.0 + {len(prefix)} chars",
                )

    def _renumber_input(self) -> None:
        self._renumber_after_id = None
        if self._updating_input or not hasattr(self, "input_text"):
            return
        current = self.input_text.get("1.0", "end").rstrip("\n")
        normalized = input_parser.normalize_input_text(current)
        if normalized != current:
            self._updating_input = True
            try:
                self.input_text.delete("1.0", "end")
                self.input_text.insert("1.0", normalized)
                self.input_text.mark_set("insert", "end-1c")
                self.input_text.see("insert")
            finally:
                self._updating_input = False
            self._save_input_cache()
        self._style_input()

    @staticmethod
    def _known_link_details(current: str) -> dict[str, tuple[int, str]]:
        known: dict[str, tuple[int, str]] = {}
        jobs, _ignored = input_parser.build_input_jobs(current)
        for seq, raw in jobs:
            if seq is None:
                continue
            for url in extractor.extract_urls(raw):
                known.setdefault(input_parser.link_identity(url), (int(seq), url))
        return known

    def _duplicate_urls_against_known(
        self, current: str, candidate: str
    ) -> list[tuple[str, int, str]]:
        known = self._known_link_details(current)
        next_seq = max((seq for seq, _url in known.values()), default=0) + 1
        duplicates: list[tuple[str, int, str]] = []
        for url in extractor.extract_urls(candidate):
            identity = input_parser.link_identity(url)
            previous = known.get(identity)
            if previous is not None:
                duplicates.append((url, previous[0], previous[1]))
            else:
                known[identity] = (next_seq, url)
                next_seq += 1
        return duplicates

    @staticmethod
    def _remove_duplicate_urls_from_paste(
        candidate: str, duplicates: list[tuple[str, int, str]]
    ) -> str:
        duplicate_urls = {new_url for new_url, _seq, _old_url in duplicates}
        kept_lines: list[str] = []
        for line in candidate.splitlines():
            hits = [url for url in extractor.extract_urls(line) if url in duplicate_urls]
            if not hits:
                kept_lines.append(line)
                continue
            cleaned = line
            for url in hits:
                cleaned = cleaned.replace(url, "")
            if extractor.extract_urls(cleaned):
                kept_lines.append(cleaned.strip())
        return "\n".join(kept_lines).strip()

    def _on_input_paste(self, _event=None):
        """只在显式粘贴时读取剪贴板，并只过滤本次粘贴的重复链接。"""
        try:
            pasted = str(self.root.clipboard_get())
        except tk.TclError:
            self._schedule_renumber()
            return None

        current = self.input_text.get("1.0", "end-1c")
        try:
            before = self.input_text.get("1.0", "sel.first")
            after = self.input_text.get("sel.last", "end-1c")
            current = before + after
        except tk.TclError:
            pass

        duplicates = self._duplicate_urls_against_known(current, pasted)
        if duplicates:
            filtered = self._remove_duplicate_urls_from_paste(pasted, duplicates)
            try:
                self.input_text.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            if filtered:
                self.input_text.insert("insert", filtered)
            self._schedule_renumber()
            self.status_var.set(
                f"已过滤本次粘贴的 {len(duplicates)} 条重复链接，其余内容已保留"
            )
            return "break"

        self._schedule_renumber()
        return None

    def _set_task_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.start_button,
            self.clear_button,
            self.output_browse_button,
        ):
            widget.config(state=state)
        self.input_text.config(state=state)
        self.output_entry.config(state=state)
        self.stop_button.config(state="disabled" if enabled else "normal")

    def browse_output(self) -> None:
        if self.running:
            return
        chosen = filedialog.askdirectory(
            title="选择轻量版输出目录",
            initialdir=self.output_var.get().strip() or default_output_dir(),
        )
        if not chosen:
            return
        self.output_var.set(chosen)
        self.output_dir = Path(chosen)
        save_config({"output_dir": str(self.output_dir)})
        self.load_existing_records()

    def open_output(self) -> None:
        path = Path(self.output_var.get().strip() or default_output_dir())
        path.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(path)

    def open_log(self) -> None:
        path = Path(self.output_var.get().strip() or default_output_dir()) / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("（暂无日志，点击“开始提取”后自动生成）\n", encoding="utf-8")
        if hasattr(os, "startfile"):
            os.startfile(path)

    def clear_input(self) -> None:
        if self.running:
            return
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", "1.")
        self._style_input()
        self._save_input_cache()
        self.status_var.set("输入已清空")

    def load_existing_records(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.visible_records.clear()
        path = Path(self.output_var.get().strip() or default_output_dir()) / "提取记录.xlsx"
        try:
            rows = light_exporter.read_records(path)
        except Exception as exc:
            self.status_var.set(f"提取记录.xlsx 无法读取：{exc}")
            return
        for seq in sorted(rows):
            record = dict(rows[seq])
            record["seq"] = seq
            item_id = self.tree.insert("", "end", values=self._tree_values(record))
            self.visible_records[item_id] = record

    @staticmethod
    def _tree_values(record: dict) -> tuple:
        return (
            record.get("seq") or "",
            record.get("aweme_id") or "",
            record.get("author_id") or "",
            record.get("title") or "无",
            record.get("tags") or "无",
            f"{_as_int(record.get('likes')):,}",
            f"{_as_int(record.get('collects')):,}",
            f"{_as_int(record.get('comments')):,}",
            record.get("status") or "—",
        )

    @staticmethod
    def _existing_link_map(rows: dict[int, dict]) -> dict[str, int]:
        result: dict[str, int] = {}
        for seq, record in rows.items():
            for url in extractor.extract_urls(record.get("raw_input") or ""):
                result.setdefault(input_parser.link_identity(url), int(seq))
        return result

    @staticmethod
    def _next_available(used: set[int]) -> int:
        candidate = max(used) + 1 if used else 1
        while candidate in used:
            candidate += 1
        return candidate

    def start(self) -> None:
        if self.running:
            return
        self._renumber_input()
        jobs, ignored = input_parser.build_input_jobs(
            self.input_text.get("1.0", "end")
        )
        if not jobs:
            self.status_var.set("请先粘贴至少一条抖音作品链接")
            return

        output_dir = Path(self.output_var.get().strip() or default_output_dir())
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status_var.set(f"输出目录不可用：{exc}")
            return

        self._save_input_cache()
        self.output_dir = output_dir
        save_config({"output_dir": str(output_dir)})
        self.logger = setup_logger(output_dir / LOG_NAME)
        self.logger.info("开始轻量版提取：%d 条，输出目录 %s", len(jobs), output_dir)
        self.running = True
        self.close_requested = False
        self.cancel_event.clear()
        self.failed_jobs.clear()
        self.success_count = 0
        self.failure_count = 0
        self.unchecked_count = 0
        self.paused_message = ""
        self._set_task_controls(False)
        self.start_button.config(text="提取中…")
        ignored_note = f"，忽略 {ignored} 行无链接内容" if ignored else ""
        self.status_var.set(f"开始处理 {len(jobs)} 条链接{ignored_note}…")
        self.worker = threading.Thread(
            target=self._work_safe,
            args=(jobs, output_dir),
            daemon=False,
        )
        self.worker.start()
        self.root.after(100, self._poll)

    def stop_task(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self.stop_button.config(state="disabled")
        self.status_var.set("正在停止任务；已完成的记录会保留…")

    def _access_context(self) -> extractor.AccessContext:
        return extractor.AccessContext(
            BROWSER_PROFILE_DIR,
            self.cancel_event,
            lambda event, message: self._post(
                "verification", {"event": event}, message
            ),
        )

    def _work_safe(self, jobs: list[tuple[int | None, str]], output_dir: Path) -> None:
        logger = self.logger
        xlsx = output_dir / "提取记录.xlsx"
        current_job: tuple[int | None, str] | None = None
        context = self._access_context()
        total = len(jobs)
        try:
            try:
                existing_rows = light_exporter.read_records(xlsx)
            except Exception as exc:
                self._post("fatal", None, f"无法读取提取记录.xlsx：{exc}")
                return

            link_map = self._existing_link_map(existing_rows)
            id_map = {
                str(record.get("aweme_id")): int(seq)
                for seq, record in existing_rows.items()
                if str(record.get("aweme_id") or "").strip()
            }
            used_seqs = set(existing_rows)

            for index, current_job in enumerate(jobs, 1):
                ensure_not_cancelled(self.cancel_event)
                input_seq, line = current_job
                if index > 1:
                    interruptible_wait(0.5 + random.random() * 0.5, self.cancel_event)
                seq_hint = int(input_seq) if input_seq is not None else self._next_available(used_seqs)
                logger.info("[%d/%d] 处理顺序候选 %d：%s", index, total, seq_hint, line[:100])
                exact_hit = next(
                    (
                        link_map[input_parser.link_identity(url)]
                        for url in extractor.extract_urls(line)
                        if input_parser.link_identity(url) in link_map
                    ),
                    None,
                )

                pause_message = ""
                try:
                    fetched, fail_status = fetch_with_retry(
                        logger, seq_hint, line, self.cancel_event, context
                    )
                except extractor.BrowserVerificationError as exc:
                    fetched, fail_status = None, exc.status
                    pause_message = str(exc)

                if fetched is None:
                    if exact_hit is not None:
                        failed_record = dict(existing_rows.get(exact_hit) or {})
                        failed_record["status"] = fail_status or "获取失败"
                        failed_record["updated_at"] = datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        try:
                            light_exporter.update_records(
                                xlsx, {int(exact_hit): failed_record}, [int(exact_hit)]
                            )
                            existing_rows[int(exact_hit)] = failed_record
                        except Exception as exc:
                            logger.error("顺序 %d 失败状态写回失败：%s", exact_hit, exc)
                    self._post(
                        "error",
                        {"job": current_job, "seq": exact_hit or seq_hint},
                        fail_status or "获取失败",
                    )
                    if pause_message:
                        self._post(
                            "paused",
                            {"unchecked": total - index},
                            f"批次已暂停：{pause_message}",
                        )
                        break
                    self._post("progress", {"done": index, "total": total})
                    continue

                hit_seq = id_map.get(fetched.aweme_id) or exact_hit
                if hit_seq is not None:
                    seq = int(hit_seq)
                    updated_existing = True
                elif input_seq is not None and int(input_seq) not in used_seqs:
                    seq = int(input_seq)
                    updated_existing = False
                else:
                    seq = self._next_available(used_seqs)
                    updated_existing = False

                record = build_light_record(fetched, line)
                try:
                    light_exporter.update_records(xlsx, {seq: record}, [seq])
                except Exception as exc:
                    logger.error("顺序 %d 元数据写回失败：%s", seq, exc)
                    self._post("error", {"job": current_job, "seq": seq}, str(exc))
                    self._post("progress", {"done": index, "total": total})
                    continue

                existing_rows[seq] = record
                used_seqs.add(seq)
                id_map[fetched.aweme_id] = seq
                for url in extractor.extract_urls(line):
                    link_map[input_parser.link_identity(url)] = seq
                logger.info(
                    "顺序 %d 成功，作品 ID %s，点赞 %d，收藏 %d，评论 %d",
                    seq,
                    fetched.aweme_id,
                    record["likes"],
                    record["collects"],
                    record["comments"],
                )
                self._post(
                    "ok",
                    {
                        **record,
                        "seq": seq,
                        "updated_existing": updated_existing,
                    },
                )
                self._post("progress", {"done": index, "total": total})
        except TaskCancelled:
            logger.info("轻量版任务已按用户请求取消")
            self._post("cancelled")
        except Exception as exc:
            logger.exception("轻量版任务异常终止")
            self._post("fatal", None, f"任务异常终止：{exc}")
        finally:
            context.close()
            self._post("done")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload, extra = self.message_queue.get_nowait()
                if kind == "verification":
                    self.status_var.set(extra or "请在弹出的浏览器中完成验证")
                    self.progress_label.config(text="等待浏览器验证…")
                elif kind == "progress":
                    total = int((payload or {}).get("total") or 0)
                    done = int((payload or {}).get("done") or 0)
                    percent = int(done / total * 100) if total else 0
                    self.progress["value"] = percent
                    self.progress_label.config(text=f"{done}/{total}")
                elif kind == "ok":
                    self.success_count += 1
                    self.progress_label.config(text=f"顺序 {payload.get('seq')} 完成")
                elif kind == "error":
                    self.failure_count += 1
                    job = (payload or {}).get("job")
                    if job and job not in self.failed_jobs:
                        self.failed_jobs.append(job)
                    self.status_var.set(
                        f"顺序 {(payload or {}).get('seq') or ''}：{extra or '提取失败'}"
                    )
                elif kind == "paused":
                    self.unchecked_count = int((payload or {}).get("unchecked") or 0)
                    self.paused_message = extra or "批次已暂停"
                    self.status_var.set(self.paused_message)
                elif kind == "cancelled":
                    self.status_var.set("任务已取消，已完成记录保留")
                elif kind == "fatal":
                    self.failure_count += 1
                    self.status_var.set(extra or "任务异常终止")
                elif kind == "done":
                    self._finish()
                    return
        except queue.Empty:
            pass
        if self.running:
            self.root.after(100, self._poll)

    def _finish(self) -> None:
        self.running = False
        self._set_task_controls(True)
        self.start_button.config(text="开始提取")
        self.progress["value"] = 0
        self.progress_label.config(text="")
        self.load_existing_records()
        summary = (
            f"完成：成功 {self.success_count}，失败 {self.failure_count}，"
            f"未检查 {self.unchecked_count}"
        )
        if self.paused_message:
            summary += f"；{self.paused_message}"
        self.status_var.set(summary)
        if self.close_requested:
            self.root.after(50, self._wait_then_close)

    def _on_close(self) -> None:
        self._save_input_cache()
        if self.running or (self.worker is not None and self.worker.is_alive()):
            if not self.close_requested:
                if not messagebox.askyesno(
                    "停止任务并退出",
                    "当前任务仍在运行。是否停止任务并退出？",
                    parent=self.root,
                ):
                    return
                self.close_requested = True
                self.stop_task()
            self.status_var.set("正在停止任务，请稍候…")
            self.root.after(100, self._wait_then_close)
            return
        self.root.destroy()

    def _wait_then_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self._wait_then_close)
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    LightDouyinApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
