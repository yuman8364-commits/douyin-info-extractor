# -*- coding: utf-8 -*-
"""抖音信息提取工具 - Windows 桌面界面（Tkinter）。"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import random
import re
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import exporter
import extractor
import input_parser
from storage import ArtifactTransaction, TransactionError
from tasking import TaskCancelled, TaskMessage, ensure_not_cancelled, interruptible_wait
from openpyxl import load_workbook
from PIL import Image, ImageTk

APP_VERSION = "2.0.3"
PREVIEW_BOX_SIZE = (190, 250)
PREVIEW_IMAGE_SIZE = (170, 230)
PREVIEW_BACKGROUND = (242, 242, 242, 255)
SOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR


def _writable_state_dir() -> Path:
    """优先使用便携 data；只读时回退到当前用户的 LocalAppData。"""
    preferred = APP_DIR / "data" if getattr(sys, "frozen", False) else SOURCE_DIR
    candidates = [preferred]
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "抖音信息提取工具")
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
STARTUP_ERROR_FILE = STATE_DIR / "启动错误.log"
LOG_NAME = "提取日志.log"
DIVIDER_RE = input_parser.DIVIDER_RE


# 输入解析逻辑已拆到纯逻辑模块；保留这些公开名称以兼容旧测试和调用。
split_entry_blocks = input_parser.split_entry_blocks
_block_raw_content = input_parser._block_raw_content
_is_placeholder_block = input_parser._is_placeholder_block
build_input_jobs = input_parser.build_input_jobs
input_job_at_line = input_parser.input_job_at_line
normalize_input_text = input_parser.normalize_input_text


def prepare_preview_image(path) -> Image.Image:
    """把封面等比例居中到固定画布，避免 Tk Label 切换图片后挤压布局。"""
    with Image.open(path) as source:
        image = source.copy()
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    image.thumbnail(PREVIEW_IMAGE_SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", PREVIEW_IMAGE_SIZE, PREVIEW_BACKGROUND)
    offset = (
        (PREVIEW_IMAGE_SIZE[0] - image.width) // 2,
        (PREVIEW_IMAGE_SIZE[1] - image.height) // 2,
    )
    if image.mode == "RGBA":
        canvas.paste(image, offset, image)
    else:
        canvas.paste(image, offset)
    return canvas.convert("RGB")


def build_cover_map(output_dir: Path, seqs) -> dict[int, str]:
    """按「封面/N.*」为给定序号建立封面路径映射。"""
    cover_map: dict[int, str] = {}
    cover_dir = output_dir / "封面"
    if cover_dir.exists():
        for seq in seqs:
            matches = sorted(cover_dir.glob(f"{seq}.*"))
            if matches:
                cover_map[seq] = str(matches[0])
    return cover_map


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8").lstrip("\ufeff"))
    except Exception:
        return {}


def load_input_cache() -> str:
    """读取上次关闭工具时保存的输入框内容（之前粘贴的链接）。"""
    try:
        return INPUT_CACHE_FILE.read_text(encoding="utf-8").lstrip("\ufeff").rstrip("\n")
    except Exception:
        return ""


def save_config(updates: dict) -> None:
    """合并写入配置，避免覆盖已有的其它设置。"""
    try:
        config = load_config()
        config.update(updates)
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def fetch_with_retry(
    logger, seq: int, link: str, cancel_event: threading.Event | None = None
):
    """统一抓取重试；返回 ``(FetchedRecord, 失败状态)``。"""
    last_error = "获取失败"
    for attempt in (1, 2):
        ensure_not_cancelled(cancel_event)
        try:
            return extractor.fetch_record(link, cancel_event), None
        except TaskCancelled:
            raise
        except extractor.InvalidLinkError:
            return None, "链接无效"
        except extractor.LoginRequiredError as exc:
            last_error = f"目标作品暂不可用（{exc}）"
        except extractor.WafBlockedError as exc:
            last_error = f"风控或网络异常（{exc}）"
        except Exception as exc:
            last_error = f"获取失败（{exc}）"
        if attempt == 1:
            logger.warning("顺序 %d：%s，等待后整体重试", seq, last_error)
            interruptible_wait(4 + random.random() * 4, cancel_event)
    return None, last_error


build_input_tasks = input_parser.build_input_tasks


def default_output_dir() -> str:
    desktop = Path.home() / "Desktop"
    return str(desktop if desktop.exists() else APP_DIR)


def next_sequence(output_dir: str | Path) -> int:
    """综合媒体、文案、封面和工作簿返回下一个可用编号。"""
    root = Path(output_dir)
    numbers: set[int] = set()

    videos = root / "爆款视频"
    if videos.exists():
        for entry in videos.iterdir():
            match = re.match(r"^(\d+)(?:\.mp4)?$", entry.name)
            if match:
                numbers.add(int(match.group(1)))

    captions = root / "文案提取"
    if captions.exists():
        for entry in captions.iterdir():
            match = re.match(r"^(\d+)\.txt$", entry.name)
            if match:
                numbers.add(int(match.group(1)))

    covers = root / "封面"
    if covers.exists():
        for entry in covers.iterdir():
            match = re.match(r"^(\d+)\.[A-Za-z0-9]+$", entry.name)
            if match:
                numbers.add(int(match.group(1)))

    try:
        numbers.update(exporter.read_records(root / "提取记录.xlsx"))
    except Exception:
        # 工作簿异常由正式任务流程报告；编号计算仍避免因此崩溃。
        pass

    return (max(numbers) + 1) if numbers else 1


def setup_logger(log_path: Path) -> logging.Logger:
    """建立带轮转的共享日志，并关闭上一批次留下的文件句柄。"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    handler = RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(handler)
    root.info("抖音信息提取工具 %s", APP_VERSION)
    return root


def find_same_size_file(
    folder: Path, size: int, exclude: Path | None = None, exclude_dir: Path | None = None
) -> str | None:
    """在 folder 下递归查找字节数等于 size 的文件，返回相对路径；没有则返回 None。

    exclude：排除单个文件；exclude_dir：排除整个子目录（本次新下载的文件不参与比较）。
    用于重复提取检测：文件大小完全相同，大概率是同一作品的重复提取。
    """
    if not folder.exists() or size <= 0:
        return None
    excluded = exclude.resolve() if exclude is not None else None
    excluded_dir = exclude_dir.resolve() if exclude_dir is not None else None
    for entry in folder.rglob("*"):
        if not entry.is_file():
            continue
        if excluded_dir is not None and entry.is_relative_to(excluded_dir):
            continue
        if excluded is not None and entry.resolve() == excluded:
            continue
        try:
            if entry.stat().st_size == size:
                return str(entry.relative_to(folder))
        except OSError:
            continue
    return None


def scan_seq_numbers(output_dir: str | Path) -> tuple[set[int], set[int], set[int]]:
    """扫描输出目录，返回 (视频编号集合, 文案编号集合, 封面编号集合)。"""
    root = Path(output_dir)
    videos: set[int] = set()
    captions: set[int] = set()
    covers: set[int] = set()

    vdir = root / "爆款视频"
    if vdir.exists():
        for entry in vdir.iterdir():
            match = re.match(r"^(\d+)(?:\.mp4)?$", entry.name)
            if match:
                videos.add(int(match.group(1)))

    cdir = root / "文案提取"
    if cdir.exists():
        for entry in cdir.iterdir():
            match = re.match(r"^(\d+)\.txt$", entry.name)
            if match:
                captions.add(int(match.group(1)))

    cover_dir = root / "封面"
    if cover_dir.exists():
        for entry in cover_dir.iterdir():
            match = re.match(r"^(\d+)\.[A-Za-z0-9]+$", entry.name)
            if match:
                covers.add(int(match.group(1)))
    return videos, captions, covers


def scan_note_numbers(output_dir: str | Path) -> set[int]:
    """扫描输出目录「爆款视频」下以序号命名的目录（图文图集），返回序号集合。"""
    root = Path(output_dir)
    vdir = root / "爆款视频"
    notes: set[int] = set()
    if vdir.exists():
        for entry in vdir.iterdir():
            if entry.is_dir() and re.fullmatch(r"\d+", entry.name):
                notes.add(int(entry.name))
    return notes


def _check_excel_order(output_dir: Path, videos: set[int]) -> bool:
    """检查表格「顺序」列是否与视频文件编号一致；不一致返回 True。"""
    path = output_dir / "提取记录.xlsx"
    if not path.exists() or not videos:
        return False
    try:
        workbook = load_workbook(path, read_only=True)
        sheet = workbook.active
        headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
        if "顺序" not in headers:
            workbook.close()
            return False
        seq_index = headers.index("顺序")
        seqs: set[int] = set()
        for row in sheet.iter_rows(min_row=2, values_only=True):
            values = list(row)
            value = values[seq_index] if seq_index < len(values) else None
            if isinstance(value, int) and value > 0:
                seqs.add(value)
        workbook.close()
    except Exception:
        return False
    return bool(seqs) and seqs != videos


class DouyinExtractorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.message_queue: queue.Queue[TaskMessage] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.refresh_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.close_requested = False
        self._retry_request = False
        self.retry_run = False
        self.running = False
        self.single_run = False
        self.refreshing = False
        self.auto_refresh = False
        self.records: dict[str, dict] = {}
        self.ok_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self.cancel_count = 0
        self.rollback_count = 0
        self.failed_jobs: list[tuple[int | None, str]] = []
        self.thumb_ref = None
        self.output_dir = Path(load_config().get("output_dir") or default_output_dir())
        self._build_ui()
        # 恢复上次关闭前粘贴的链接，光标停在最新编号处，可直接继续粘贴
        cached = load_input_cache()
        if cached.strip() and cached.strip() != "1.":
            self.input_text.delete("1.0", "end")
            self.input_text.insert("1.0", cached)
        self._enforce_input_sequences()
        self.input_text.mark_set("insert", "end-1c")
        self.input_text.see("insert")
        self.load_existing_records()
        # 恢复旧行为：输入缓存中已有且媒体仍存在的记录，启动后直接安全刷新元数据。
        self.root.after(800, self._auto_refresh_existing_records)

    def _post(self, kind: str, payload=None, extra=None) -> None:
        self.message_queue.put(TaskMessage(kind, payload, extra))

    def _save_input_cache(self) -> None:
        """把输入框当前内容（之前粘贴的链接）保存到缓存文件。"""
        try:
            content = self.input_text.get("1.0", "end").rstrip("\n")
            INPUT_CACHE_FILE.write_text(content + "\n", encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        self._save_input_cache()
        active = self.running or self.refreshing or any(
            thread is not None and thread.is_alive()
            for thread in (self.worker, self.refresh_thread)
        )
        if active:
            if not self.close_requested:
                if not messagebox.askyesno(
                    "停止任务并退出",
                    "当前任务仍在运行。是否安全停止任务并在清理完成后退出？",
                    parent=self.root,
                ):
                    return
                self.close_requested = True
                self.stop_task()
            self.status_var.set("正在停止任务并清理临时文件，请稍候…")
            self.root.after(100, self._wait_then_close)
            return
        self.root.destroy()

    def _wait_then_close(self) -> None:
        threads = (self.worker, self.refresh_thread)
        if any(thread is not None and thread.is_alive() for thread in threads):
            self.root.after(100, self._wait_then_close)
            return
        self.root.destroy()

    def _set_task_controls(self, enabled: bool) -> None:
        """统一启用/禁用主操作及会改变任务输入或输出目录的控件。"""
        state = "normal" if enabled else "disabled"
        for button in (
            self.start_button,
            self.single_button,
            self.update_button,
            self.output_browse_button,
        ):
            button.config(state=state)
        self.input_text.config(state=state)
        self.output_entry.config(state=state)
        self.file_menu.entryconfig("选择输出目录…", state=state)
        self.file_menu.entryconfig("从已有文档导入链接…", state=state)
        self.edit_menu.entryconfig("清空输入", state=state)
        retry_state = "normal" if enabled and self.failed_jobs else "disabled"
        self.edit_menu.entryconfig("重试失败项", state=retry_state)
        self.stop_button.config(state="disabled" if enabled else "normal")

    def _build_ui(self) -> None:
        self.root.title("抖音信息提取工具")
        self.root.geometry("1120x680")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        menu_bar = tk.Menu(self.root)
        self.file_menu = tk.Menu(menu_bar, tearoff=False)
        self.file_menu.add_command(label="选择输出目录…", command=self.browse_output)
        self.file_menu.add_command(
            label="从已有文档导入链接…", command=self.import_links_from_workbook
        )
        self.file_menu.add_separator()
        self.file_menu.add_command(label="打开输出目录", command=self.open_output)
        self.file_menu.add_command(label="打开封面文件夹", command=self.open_covers)
        self.file_menu.add_command(label="打开日志", command=self.open_log)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="退出", command=self._on_close)
        menu_bar.add_cascade(label="文件", menu=self.file_menu)

        self.edit_menu = tk.Menu(menu_bar, tearoff=False)
        self.edit_menu.add_command(label="清空输入", command=self.clear_input)
        self.edit_menu.add_command(label="复制选中标题", command=self.copy_title)
        self.edit_menu.add_separator()
        self.edit_menu.add_command(label="重试失败项", command=self.retry_failed, state="disabled")
        menu_bar.add_cascade(label="编辑", menu=self.edit_menu)
        self.root.config(menu=menu_bar)

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        input_header = ttk.Frame(main)
        input_header.pack(fill="x")
        ttk.Label(
            input_header,
            text="粘贴抖音链接（序号 1. 2. 3. … 已锁定不可修改，只修改序号后面的原始链接/分享文案；"
            "粘贴完自动弹出下一个序号）:",
        ).pack(side="left", anchor="w")

        input_frame = ttk.Frame(main)
        input_frame.pack(fill="x", pady=(4, 8))
        self.input_text = tk.Text(
            input_frame, height=7, wrap="word", font=("Microsoft YaHei UI", 10)
        )
        scroll = ttk.Scrollbar(input_frame, orient="vertical", command=self.input_text.yview)
        self.input_text.configure(yscrollcommand=scroll.set)
        self.input_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 自动编号：默认 1. ，粘贴完毕后自动弹出下一个序号，条目间用分割线分隔；
        # 序号在每次输入变化后按位置强制恢复，用户只能改后面的原始链接。
        self._renumber_after_id = None
        self.input_text.tag_configure("seq", foreground="#8a8a8a", background="#f4f4f4")
        self.input_text.tag_configure("divider", foreground="#b0b0b0")
        self.input_text.insert("1.0", "1. ")
        self.input_text.bind("<KeyRelease>", self._schedule_renumber)
        self.input_text.bind("<<Paste>>", self._schedule_renumber)
        self.input_text.bind("<<Cut>>", self._schedule_renumber)

        output_frame = ttk.Frame(main)
        output_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(output_frame, text="输出目录:").pack(side="left")
        self.output_var = tk.StringVar(value=str(self.output_dir))
        self.output_entry = ttk.Entry(output_frame, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.output_browse_button = ttk.Button(
            output_frame, text="选择目录…", command=self.browse_output
        )
        self.output_browse_button.pack(side="right")

        button_frame = ttk.Frame(main)
        button_frame.pack(fill="x", pady=(0, 8))
        for column in range(3):
            button_frame.columnconfigure(column, weight=1, uniform="main_actions")
        self.start_button = ttk.Button(button_frame, text="全部提取", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.single_button = ttk.Button(
            button_frame, text="单次提取", command=self.extract_current_once
        )
        self.single_button.grid(row=0, column=1, sticky="ew", padx=4)
        self.update_button = ttk.Button(
            button_frame, text="强制刷新记录", command=self.refresh_table_data
        )
        self.update_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        progress_frame = ttk.Frame(main)
        progress_frame.pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(progress_frame, text="", width=30, anchor="e")
        self.progress_label.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(
            progress_frame, text="停止任务", command=self.stop_task, state="disabled"
        )
        self.stop_button.pack(side="right", padx=(6, 0))

        result_frame = ttk.Frame(main)
        result_frame.pack(fill="both", expand=True)

        columns = ("title", "likes", "comments", "media", "status")
        self.tree = ttk.Treeview(result_frame, columns=columns, show="headings")
        headings = {
            "title": ("标题", 300),
            "likes": ("点赞数", 90),
            "comments": ("评论数", 90),
            "media": ("视频/图集", 140),
            "status": ("状态", 240),
        }
        for column, (text, width) in headings.items():
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)

        tree_scroll = ttk.Scrollbar(result_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.preview_frame = ttk.Frame(
            result_frame,
            width=PREVIEW_BOX_SIZE[0],
            height=PREVIEW_BOX_SIZE[1],
        )
        self.preview_frame.pack(side="left", anchor="n", padx=(8, 0))
        self.preview_frame.pack_propagate(False)
        self.preview = tk.Label(
            self.preview_frame,
            text="选中一行查看封面预览",
            bg="#f2f2f2",
            relief="groove",
            anchor="center",
            justify="center",
        )
        self.preview.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(
            value="就绪：粘贴链接，选择输出目录后点击「全部提取」"
            "（自动检测重复、校验顺序、记录日志；重复内容在后台静默更新）"
        )
        ttk.Label(main, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(8, 0))

    def load_existing_records(self) -> None:
        """从输出目录的表格与文件加载已有记录，显示到结果列表（下次打开也能看到）。"""
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.records.clear()
        output_dir = Path(self.output_var.get().strip() or default_output_dir())
        try:
            rows = exporter.read_records(output_dir / "提取记录.xlsx")
        except Exception as exc:
            self.status_var.set(f"记录表无法读取：{exc}（原文件未修改）")
            return
        videos_dir = output_dir / "爆款视频"
        cover_dir = output_dir / "封面"
        for seq in sorted(rows):
            rec = rows[seq]
            media_display = "未下载"
            if videos_dir.exists():
                video_file = videos_dir / f"{seq}.mp4"
                image_dir = videos_dir / str(seq)
                if video_file.exists():
                    media_display = f"{seq}.mp4"
                elif image_dir.is_dir():
                    count = sum(1 for f in image_dir.iterdir() if f.is_file())
                    media_display = f"{seq}/（{count} 张图）"
            cover_path = None
            if cover_dir.exists():
                matches = sorted(cover_dir.glob(f"{seq}.*"))
                if matches:
                    cover_path = str(matches[0])
            item_id = self.tree.insert(
                "",
                "end",
                values=(
                    rec.get("title") or "无",
                    f"{rec.get('likes') or 0:,}",
                    f"{rec.get('comments') or 0:,}",
                    media_display,
                    rec.get("status") or "—",
                ),
            )
            self.records[item_id] = {
                "title": rec.get("title") or "",
                "likes": rec.get("likes") or 0,
                "comments": rec.get("comments") or 0,
                "media_display": media_display,
                "cover_path": cover_path,
                "seq": seq,
                "raw_input": rec.get("raw_input") or "",
                "status": rec.get("status") or "",
            }

    def _style_input_sequences(self) -> None:
        """把锁定序号与分割线标灰，提示这两部分不可编辑（内容可编辑）。"""
        widget = self.input_text
        widget.tag_remove("seq", "1.0", "end")
        widget.tag_remove("divider", "1.0", "end")
        expected = 0
        at_block_start = True
        for line_no, line in enumerate(widget.get("1.0", "end").splitlines(), 1):
            stripped = line.strip()
            if DIVIDER_RE.match(stripped):
                widget.tag_add("divider", f"{line_no}.0", f"{line_no}.end")
                at_block_start = True
            elif stripped and at_block_start:
                expected += 1
                prefix = f"{expected}."
                widget.tag_add("seq", f"{line_no}.0", f"{line_no}.0 + {len(prefix)} chars")
                at_block_start = False

    def _enforce_input_sequences(self) -> None:
        """强制序号 = 条目位置。

        无论用户怎么改序号，松手/粘贴后都会恢复为 1. 2. 3. …；
        原始链接/分享文案原样保留。这样第 5 条就是 5.，不会再被改成 9.
        而导致后续识别错位。
        """
        widget = self.input_text
        current = widget.get("1.0", "end").rstrip("\n")
        normalized = normalize_input_text(current)
        if normalized == current:
            self._style_input_sequences()
            return
        widget.delete("1.0", "end")
        widget.insert("1.0", normalized)
        widget.mark_set("insert", "end-1c")
        widget.see("insert")
        self._style_input_sequences()
        self._save_input_cache()

    @staticmethod
    def _build_existing_link_map(rows: dict[int, dict]) -> dict[str, int]:
        """表格已有链接 → 顺序。"""
        link_map: dict[str, int] = {}
        for seq, rec in rows.items():
            for url in extractor.extract_urls(rec.get("raw_input") or ""):
                link_map.setdefault(url, seq)
        return link_map

    def _existing_media_targets(
        self, jobs: list[tuple[int | None, str]], output_dir: Path, rows: dict[int, dict]
    ) -> list[tuple[int, str]]:
        """找出输入里已提取过且视频/图集还在的链接，返回 [(顺序, 原始链接)]。"""
        link_map = self._build_existing_link_map(rows)
        videos_dir = output_dir / "爆款视频"
        targets: list[tuple[int, str]] = []
        seen: set[int] = set()
        for _seq, raw in jobs:
            for url in extractor.extract_urls(raw):
                hit_seq = link_map.get(url)
                if not hit_seq or hit_seq in seen:
                    continue
                if (videos_dir / f"{hit_seq}.mp4").exists() or (
                    videos_dir / str(hit_seq)
                ).is_dir():
                    targets.append((hit_seq, raw))
                    seen.add(hit_seq)
                    break
        return targets

    def _auto_refresh_existing_records(self) -> None:
        """启动后直接刷新输入中已提取过的记录，不重复下载媒体。"""
        if self.running or self.refreshing:
            return
        self._enforce_input_sequences()
        output_dir = Path(self.output_var.get().strip() or default_output_dir())
        xlsx = output_dir / "提取记录.xlsx"
        if not xlsx.exists():
            return
        try:
            rows = exporter.read_records(xlsx)
        except Exception:
            return
        jobs, _ignored = build_input_jobs(self.input_text.get("1.0", "end"))
        targets = self._existing_media_targets(jobs, output_dir, rows)
        if not targets:
            return
        refresh_targets = [(seq, rows[seq]) for seq, _raw in targets if seq in rows]
        if refresh_targets:
            self._start_table_refresh(output_dir, refresh_targets, automatic=True)

    def browse_output(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择输出目录", initialdir=self.output_var.get() or default_output_dir()
        )
        if not chosen:
            return
        self.output_var.set(chosen)
        self.output_dir = Path(chosen)
        save_config({"output_dir": str(self.output_dir)})
        self.load_existing_records()

    def import_links_from_workbook(self) -> None:
        """只读旧提取记录，按「顺序」把其中的纯链接回填到输入框。"""
        if self.running or self.refreshing:
            return
        initial_dir = self.output_var.get().strip() or str(default_output_dir())
        chosen = filedialog.askopenfilename(
            title="选择以前的提取记录文档",
            initialdir=initial_dir,
            filetypes=(
                ("Excel 工作簿", ("*.xlsx", "*.xlsm")),
                ("所有文件", "*.*"),
            ),
        )
        if not chosen:
            return

        path = Path(chosen)
        logger = logging.getLogger("douyin_tool")
        try:
            rows = exporter.read_records(path)
        except Exception as exc:
            logger.warning("导入旧文档失败：%s：%s", path.name, exc)
            messagebox.showerror(
                "无法读取文档",
                f"无法读取“{path.name}”：\n{exc}\n\n原文档和当前输入均未修改。",
                parent=self.root,
            )
            return

        links = input_parser.links_from_records_in_sequence(rows)
        if not links:
            messagebox.showinfo(
                "没有可导入的链接",
                "文档中没有同时具备有效“顺序”和抖音链接的记录。\n"
                "请选择本工具以前生成的提取记录.xlsx。",
                parent=self.root,
            )
            return

        current_links = extractor.extract_urls(self.input_text.get("1.0", "end"))
        if current_links and not messagebox.askyesno(
            "替换当前输入",
            f"当前输入框已有 {len(current_links)} 条链接。\n"
            f"是否替换为“{path.name}”中的 {len(links)} 条链接？",
            parent=self.root,
        ):
            self.status_var.set("已取消导入，当前输入未修改")
            return

        formatted = input_parser.format_ordered_links(links)
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", formatted)
        self.input_text.mark_set("insert", "end-1c")
        self.input_text.see("insert")
        self._style_input_sequences()
        self._save_input_cache()
        self.status_var.set(
            f"已从“{path.name}”按文档顺序导入 {len(links)} 条链接（未联网、未修改原文档）"
        )
        logger.info("从旧文档导入链接：%s，共 %d 条", path.name, len(links))

    def start(self, jobs_override: list[tuple[int | None, str]] | None = None) -> None:
        if self.running or self.refreshing:
            return
        retry_run = bool(jobs_override is not None and self._retry_request)
        self._retry_request = False
        single_run = jobs_override is not None and not retry_run
        if jobs_override is not None:
            jobs = list(jobs_override or [])
            ignored = 0
        else:
            self._enforce_input_sequences()
            jobs, ignored = build_input_jobs(self.input_text.get("1.0", "end"))
        if not jobs:
            self.status_var.set("请先粘贴至少一条抖音链接")
            return
        self._save_input_cache()

        output_dir = Path(self.output_var.get().strip() or default_output_dir())
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "爆款视频").mkdir(parents=True, exist_ok=True)
            (output_dir / "文案提取").mkdir(parents=True, exist_ok=True)
            (output_dir / "封面").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status_var.set(f"输出目录不可用：{exc}")
            return
        self.output_dir = output_dir
        save_config({"output_dir": str(output_dir)})
        self.logger = setup_logger(output_dir / LOG_NAME)
        self.logger.info("=" * 60)
        action_name = "重试失败项" if retry_run else ("单次提取" if single_run else "新批次")
        self.logger.info("开始%s：共 %d 条，输出目录 %s", action_name, len(jobs), output_dir)

        # —— 多重检测 1：提取前检查编号一致性，能自动修的立即修，需确认的弹窗提示 ——
        audit_note = ""
        videos_n, captions_n, covers_n = scan_seq_numbers(output_dir)
        note_n = scan_note_numbers(output_dir)
        # 图文（图集目录）不生成文案文件，只给视频补建空文案
        for n in sorted((videos_n - note_n) - captions_n):
            try:
                (output_dir / "文案提取" / f"{n}.txt").write_text("", encoding="utf-8")
                self.logger.warning("顺序检测：序号 %d 有视频缺文案，已自动补建空文案文件", n)
            except OSError as exc:
                self.logger.error("顺序检测：补建 %d.txt 失败：%s", n, exc)
        orphan_captions = captions_n - videos_n
        orphan_covers = covers_n - videos_n
        excel_mismatch = _check_excel_order(output_dir, videos_n)
        if orphan_captions or orphan_covers or excel_mismatch:
            notes = [
                f"· 序号 {n}：只有文案文件 {n}.txt，没有对应视频" for n in sorted(orphan_captions)
            ]
            notes += [
                f"· 序号 {n}：只有封面文件，没有对应视频" for n in sorted(orphan_covers)
            ]
            if excel_mismatch:
                notes.append("· 表格「顺序」列与文件编号不一致，请手动核对")
            msg = (
                "提取前检测到顺序问题：\n"
                + "\n".join(notes)
                + "\n\n多余文件是否移入「顺序修复备份」文件夹以修正顺序？"
                "\n（选「否」保留原样，编号沿用旧最大值）"
            )
            if messagebox.askyesno("顺序检测", msg, parent=self.root):
                backup_dir = output_dir / "顺序修复备份" / time.strftime("%Y-%m-%d_%H%M%S")
                backup_dir.mkdir(parents=True, exist_ok=True)
                for n in sorted(orphan_captions):
                    src = output_dir / "文案提取" / f"{n}.txt"
                    if src.exists():
                        shutil.move(str(src), str(backup_dir / f"{n}.txt"))
                for n in sorted(orphan_covers):
                    for src in (output_dir / "封面").glob(f"{n}.*"):
                        shutil.move(str(src), str(backup_dir / src.name))
                self.logger.warning(
                    "已按用户确认将多余文件移入「顺序修复备份」（文案 %d 个、封面 %d 个）",
                    len(orphan_captions),
                    len(orphan_covers),
                )
                audit_note = "，多余文件已移入「顺序修复备份」"
            else:
                self.logger.warning("检测到顺序问题但用户选择保留原样")
                audit_note = "，⚠ 顺序问题未处理"

        self.running = True
        self.single_run = single_run
        self.retry_run = retry_run
        self.cancel_event.clear()
        self.close_requested = False
        self.ok_count = 0
        self.fail_count = 0
        self.skip_count = 0
        self.cancel_count = 0
        self.rollback_count = 0
        self.failed_jobs = []

        self._set_task_controls(False)
        if single_run:
            self.single_button.config(text="提取中…")
        else:
            self.start_button.config(text="提取中…")
        ignore_note = f"，忽略 {ignored} 行无链接内容" if ignored else ""
        if single_run:
            self.status_var.set(f"正在单次提取当前链接{audit_note}…")
        elif retry_run:
            self.status_var.set(f"正在重试 {len(jobs)} 条失败项{audit_note}…")
        else:
            self.status_var.set(f"开始提取，共 {len(jobs)} 条{audit_note}{ignore_note}…")
        self.worker = threading.Thread(
            target=self._work_safe,
            args=(jobs, output_dir),
            daemon=False,
        )
        self.worker.start()
        self.root.after(100, self._poll)

    def _work_safe(
        self,
        jobs: list[tuple[int | None, str]],
        output_dir: Path,
    ) -> None:
        """带取消、稳定 ID 去重和文件事务的正式提取流程。"""
        logger = logging.getLogger("douyin_tool")
        videos_dir = output_dir / "爆款视频"
        xlsx = output_dir / "提取记录.xlsx"
        current_job: tuple[int | None, str] | None = None

        def has_media(seq: int) -> bool:
            return (videos_dir / f"{seq}.mp4").is_file() or (videos_dir / str(seq)).is_dir()

        try:
            existing_rows = exporter.read_records(xlsx)
            link_map = self._build_existing_link_map(existing_rows)
            id_map = {
                str(rec.get("aweme_id")): seq
                for seq, rec in existing_rows.items()
                if str(rec.get("aweme_id") or "").strip()
            }
            used_seqs = set(existing_rows)
            used_seqs.update(scan_seq_numbers(output_dir)[0])

            normalized_jobs = (
                [(None, line) for line in jobs] if jobs and isinstance(jobs[0], str) else jobs
            )
            for index, current_job in enumerate(normalized_jobs, 1):
                ensure_not_cancelled(self.cancel_event)
                input_seq, line = current_job
                if index > 1:
                    interruptible_wait(1 + random.random(), self.cancel_event)
                seq_hint = input_seq if input_seq is not None else ((max(used_seqs) + 1) if used_seqs else 1)
                logger.info("[%d/%d] 抓取顺序候选 %d：%s", index, len(normalized_jobs), seq_hint, line[:80])

                fetched, fail_status = fetch_with_retry(
                    logger, seq_hint, line, self.cancel_event
                )
                if fetched is None:
                    self._post(
                        "error",
                        {"input": line, "job": current_job, "seq": seq_hint},
                        fail_status,
                    )
                    continue

                exact_hit = next(
                    (
                        link_map[url]
                        for url in extractor.extract_urls(line)
                        if url in link_map
                    ),
                    None,
                )
                id_hit = id_map.get(fetched.aweme_id)
                hit_seq = id_hit or exact_hit

                # 同一作品且媒体仍在：只更新元数据，不重复下载。
                if hit_seq is not None and has_media(hit_seq):
                    updated = dict(existing_rows.get(hit_seq) or {})
                    updated.update(
                        {
                            "raw_input": line,
                            "title": fetched.fields["title"],
                            "tags": fetched.fields["tags"],
                            "likes": fetched.fields["likes"],
                            "comments": fetched.fields["comments"],
                            "author": fetched.fields["author"],
                            "status": "正常",
                            "aweme_id": fetched.aweme_id,
                            "work_kind": "图文" if fetched.kind == "note" else "视频",
                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    try:
                        exporter.update_records(xlsx, {hit_seq: updated}, [hit_seq])
                    except Exception as exc:
                        logger.error("顺序 %d 元数据写回失败：%s", hit_seq, exc)
                        self._post(
                            "error",
                            {"input": line, "job": current_job, "seq": hit_seq},
                            str(exc),
                        )
                        continue
                    existing_rows[hit_seq] = updated
                    id_map[fetched.aweme_id] = hit_seq
                    for url in extractor.extract_urls(line):
                        link_map[url] = hit_seq
                    logger.info("作品 %s 已存在，顺序 %d 只更新元数据", fetched.aweme_id, hit_seq)
                    self._post("skip", {"seq": hit_seq, "reason": "同作品元数据已更新"})
                    continue

                if hit_seq is not None:
                    seq = hit_seq
                elif input_seq is not None:
                    seq = input_seq
                else:
                    seq = (max(used_seqs) + 1) if used_seqs else 1
                replacing = seq in existing_rows or has_media(seq)
                transaction = ArtifactTransaction(output_dir, seq)
                try:
                    if fetched.kind == "note":
                        staged_media = transaction.note_target()

                        def image_progress(done, total, n=seq):
                            self._post(
                                "progress",
                                {"seq": n, "done": done, "total": total, "unit": "images"},
                            )

                        paths = extractor.download_images(
                            fetched.session,
                            fetched.item,
                            staged_media,
                            image_progress,
                            self.cancel_event,
                        )
                        hits = [
                            find_same_size_file(videos_dir, path.stat().st_size)
                            for path in paths
                        ]
                        if paths and all(hits):
                            logger.warning(
                                "作品 %s 的图集大小与旧文件相似，仅记录提醒，不自动判重",
                                fetched.aweme_id,
                            )
                        media_display = f"{seq}/（{len(paths)} 张图）"
                    else:
                        staged_media = transaction.video_target()

                        def video_progress(done, total, n=seq):
                            self._post(
                                "progress",
                                {"seq": n, "done": done, "total": total, "unit": "bytes"},
                            )

                        extractor.download_video(
                            fetched.session,
                            fetched.item,
                            staged_media,
                            video_progress,
                            cancel_event=self.cancel_event,
                        )
                        size_hit = find_same_size_file(
                            videos_dir, staged_media.stat().st_size
                        )
                        if size_hit:
                            logger.warning(
                                "作品 %s 与 %s 大小相同，仅记录提醒，不自动判重",
                                fetched.aweme_id,
                                size_hit,
                            )
                        media_display = f"{seq}.mp4"

                    staged_cover = None
                    if fetched.fields.get("cover_url"):
                        try:
                            staged_cover = extractor.download_cover(
                                fetched.session,
                                fetched.fields["cover_url"],
                                transaction.cover_dir(),
                                str(seq),
                                self.cancel_event,
                            )
                        except TaskCancelled:
                            raise
                        except Exception:
                            fresh_session, fresh_item = extractor.fetch_item_with_session(
                                fetched.session,
                                fetched.aweme_id,
                                fetched.kind,
                                self.cancel_event,
                            )
                            fresh_fields = extractor.extract_fields(
                                fresh_item, fetched.aweme_id
                            )
                            if not fresh_fields.get("cover_url"):
                                raise extractor.ExtractionError("封面地址不可用")
                            staged_cover = extractor.download_cover(
                                fresh_session,
                                fresh_fields["cover_url"],
                                transaction.cover_dir(),
                                str(seq),
                                self.cancel_event,
                            )

                    old_record = existing_rows.get(seq) or {}
                    record = {
                        "raw_input": line,
                        "title": fetched.fields["title"],
                        "tags": fetched.fields["tags"],
                        "likes": fetched.fields["likes"],
                        "comments": fetched.fields["comments"],
                        "type": old_record.get("type") or "基本盘",
                        "author": fetched.fields["author"],
                        "status": "正常",
                        "aweme_id": fetched.aweme_id,
                        "work_kind": "图文" if fetched.kind == "note" else "视频",
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "media_display": media_display,
                        "seq": seq,
                        "replace": replacing,
                    }

                    def persist_workbook(final_cover: Path | None) -> None:
                        latest_rows = exporter.read_records(xlsx)
                        latest_manual = latest_rows.get(seq) or {}
                        if latest_manual.get("type"):
                            record["type"] = latest_manual["type"]
                        latest_rows[seq] = record
                        cover_map = build_cover_map(output_dir, set(latest_rows))
                        cover_map[seq] = str(final_cover) if final_cover else None
                        exporter.update_records(xlsx, latest_rows, [seq], cover_map)

                    result = transaction.commit(
                        work_kind=fetched.kind,
                        staged_media=staged_media,
                        staged_cover=staged_cover,
                        persist_workbook=persist_workbook,
                    )
                    record["cover_path"] = str(result.cover_path) if result.cover_path else None
                    existing_rows[seq] = dict(record)
                    used_seqs.add(seq)
                    id_map[fetched.aweme_id] = seq
                    for url in extractor.extract_urls(line):
                        link_map[url] = seq
                    logger.info("顺序 %d 成功，作品 ID %s：%s", seq, fetched.aweme_id, fetched.fields["title"][:40])
                    self._post("ok", record, "")
                except TaskCancelled:
                    transaction.cleanup()
                    raise
                except TransactionError as exc:
                    transaction.cleanup()
                    self._post(
                        "error",
                        {
                            "input": line,
                            "job": current_job,
                            "seq": seq,
                            "rolled_back": exc.rolled_back,
                        },
                        str(exc),
                    )
                except Exception as exc:
                    transaction.cleanup()
                    logger.error("顺序 %d 处理失败：%s", seq, exc)
                    self._post(
                        "error",
                        {"input": line, "job": current_job, "seq": seq},
                        str(exc),
                    )
        except TaskCancelled:
            logger.info("任务已按用户请求取消")
            self._post("cancelled", {"job": current_job})
        except Exception as exc:
            logger.exception("提取任务异常终止")
            self._post(
                "fatal",
                {"job": current_job},
                f"任务异常终止，原有文件未主动修改：{exc}",
            )
        finally:
            self._post("done")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload, extra = self.message_queue.get_nowait()
                if kind == "ok":
                    self.ok_count += 1
                    self.progress["value"] = 100
                    self.progress_label.config(text=f"{payload['seq']} 完成")
                    # 原位替换不往页面重复插行，批次结束后统一重载表格
                    if not payload.get("replace"):
                        item_id = self.tree.insert(
                            "",
                            "end",
                            values=(
                                payload["title"],
                                f"{payload['likes']:,}",
                                f"{payload['comments']:,}",
                                payload["media_display"],
                                "成功" + (extra or ""),
                            ),
                        )
                        self.records[item_id] = payload
                    continue
                if kind == "skip":
                    self.skip_count += 1
                    self.status_var.set(
                        f"序号 {payload.get('seq')}：{payload.get('reason') or '已更新，不重复下载'}"
                    )
                    continue
                if kind == "dup_status":
                    self.skip_count += payload.get("total") or 0
                    self.status_var.set(
                        f"后台静默更新 {payload.get('total') or 0} 条重复数据，"
                        "不重复显示在页面…"
                    )
                    continue
                if kind == "progress":
                    total = payload.get("total") or 0
                    done = payload.get("done") or 0
                    percent = int(done / total * 100) if total else 0
                    self.progress["value"] = percent
                    if payload.get("unit") == "images":
                        text = f"下载图集 {payload['seq']}：{done}/{total} 张"
                    elif total:
                        text = f"下载视频 {payload['seq']}：{done // 1048576}MB/{total // 1048576}MB"
                    else:
                        text = f"下载视频 {payload['seq']}：{done // 1048576}MB"
                    self.progress_label.config(text=text)
                elif kind == "error":
                    self.fail_count += 1
                    if payload.get("rolled_back"):
                        self.rollback_count += 1
                    job = payload.get("job")
                    if job and job not in self.failed_jobs:
                        self.failed_jobs.append(job)
                    input_text = payload.get("input") or ""
                    item_id = self.tree.insert(
                        "", "end", values=(input_text[:40], "", "", "", extra)
                    )
                    self.records[item_id] = {"title": "", "input": input_text, "message": extra}
                elif kind == "cancelled":
                    self.cancel_count += 1
                    self.status_var.set("任务已取消，已完成当前清理")
                elif kind == "fatal":
                    self.fail_count += 1
                    job = payload.get("job") if isinstance(payload, dict) else None
                    if job and job not in self.failed_jobs:
                        self.failed_jobs.append(job)
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
        self.start_button.config(text="全部提取")
        self.single_button.config(text="单次提取")
        self.progress["value"] = 0
        self.progress_label.config(text="")
        self.load_existing_records()
        prefix = (
            "失败项重试完成"
            if getattr(self, "retry_run", False)
            else ("单次提取完成" if getattr(self, "single_run", False) else "完成")
        )
        summary = (
            f"{prefix}：成功 {self.ok_count}，同作品更新 {self.skip_count}，"
            f"失败 {self.fail_count}，取消 {self.cancel_count}，已回滚 {self.rollback_count}"
        )
        self.status_var.set(summary)
        logger = logging.getLogger("douyin_tool")
        logger.info(
            "批次完成：成功 %d，同作品更新 %d，失败 %d，取消 %d，已回滚 %d",
            self.ok_count,
            self.skip_count,
            self.fail_count,
            self.cancel_count,
            self.rollback_count,
        )
        # —— 多重检测 3：批次结束后复核顺序一致性 ——
        try:
            videos_n, captions_n, covers_n = scan_seq_numbers(self.output_dir)
            note_n = scan_note_numbers(self.output_dir)
            video_seqs = videos_n - note_n
            if video_seqs - captions_n:
                logger.warning("批次后复核：序号 %s 缺文案文件", sorted(video_seqs - captions_n))
            if captions_n - videos_n:
                logger.warning("批次后复核：序号 %s 只有文案没有视频", sorted(captions_n - videos_n))
            if covers_n - videos_n:
                logger.warning("批次后复核：序号 %s 只有封面没有视频", sorted(covers_n - videos_n))
        except Exception as exc:
            logger.warning("批次后复核失败：%s", exc)
        if self.close_requested:
            self.root.after(50, self._wait_then_close)

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        record = self.records.get(selection[0])
        cover_path = record.get("cover_path") if record else None
        try:
            image = prepare_preview_image(cover_path)
            self.thumb_ref = ImageTk.PhotoImage(image)
            self.preview.config(image=self.thumb_ref, text="")
        except Exception:
            self.thumb_ref = None
            self.preview.config(image="", text="封面不可用")

    def copy_title(self) -> None:
        selection = self.tree.selection()
        if selection:
            record = self.records.get(selection[0])
        else:
            children = self.tree.get_children()
            record = self.records.get(children[-1]) if children else None
        title = (record or {}).get("title")
        if not title:
            self.status_var.set("没有可复制的标题")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(title)
        self.status_var.set("标题已复制")

    def stop_task(self) -> None:
        """请求当前提取或更新任务安全停止。"""
        if not (self.running or self.refreshing):
            return
        self.cancel_event.set()
        self.stop_button.config(state="disabled")
        self.status_var.set("正在停止任务；已完成的记录会保留，当前暂存内容将清理…")

    def retry_failed(self) -> None:
        """只重新执行上一次失败的输入项。"""
        if self.running or self.refreshing:
            return
        jobs = list(self.failed_jobs)
        if not jobs:
            self.status_var.set("没有可重试的失败项")
            return
        self._retry_request = True
        self.start(jobs_override=jobs)

    def extract_current_once(self) -> None:
        """立即且只执行一次光标所在（或最近一条）链接的完整提取流程。"""
        if self.running or self.refreshing:
            self.status_var.set("正在提取/刷新中，请等当前任务完成后再单次提取")
            return
        text = self.input_text.get("1.0", "end")
        try:
            cursor_line = int(self.input_text.index("insert").split(".", 1)[0])
        except (AttributeError, TypeError, ValueError):
            cursor_line = len(text.splitlines()) or 1
        job = input_job_at_line(text, cursor_line)
        if job is None:
            self.status_var.set("当前及前面的条目中没有可单次提取的抖音链接")
            return
        self._enforce_input_sequences()
        self.start(jobs_override=[job])

    def open_covers(self) -> None:
        covers_dir = Path(self.output_var.get().strip() or default_output_dir()) / "封面"
        covers_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(covers_dir)

    def open_log(self) -> None:
        log_path = Path(self.output_var.get().strip() or default_output_dir()) / LOG_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_text("（暂无日志，点击「全部提取」后自动生成）\n", encoding="utf-8")
        os.startfile(log_path)

    def open_output(self) -> None:
        target = Path(self.output_var.get().strip() or default_output_dir())
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def refresh_table_data(self) -> None:
        """按表格里的全部链接直接强制抓取最新数据。"""
        if self.running or self.refreshing:
            self.status_var.set("正在提取/更新中，请等当前任务完成")
            return
        output_dir = Path(self.output_var.get().strip() or default_output_dir())
        xlsx = output_dir / "提取记录.xlsx"
        if not xlsx.exists():
            self.status_var.set("输出目录里没有「提取记录.xlsx」")
            return
        rows = exporter.read_records(xlsx)
        targets = [
            (seq, rec)
            for seq, rec in sorted(rows.items())
            if (rec.get("raw_input") or "").strip()
        ]
        if not targets:
            self.status_var.set("表格里没有带链接的行，无法刷新")
            return

        self._start_table_refresh(output_dir, targets, automatic=False)

    def _start_table_refresh(
        self, output_dir: Path, targets: list, *, automatic: bool
    ) -> None:
        """启动安全的表格强制刷新；automatic 用于恢复旧版启动自动刷新。"""
        setup_logger(output_dir / LOG_NAME)
        mode = "启动自动强制刷新" if automatic else "手动强制刷新"
        logging.getLogger("douyin_tool").info("%s：共 %d 行有链接", mode, len(targets))
        self.refreshing = True
        self.auto_refresh = automatic
        self.cancel_event.clear()
        self.close_requested = False
        self.failed_jobs = []
        self.cancel_count = 0
        self.rollback_count = 0
        self._set_task_controls(False)
        self.update_button.config(text="刷新中…")
        prefix = "启动后正在直接刷新已有记录" if automatic else "开始强制刷新记录"
        self.status_var.set(f"{prefix}，共 {len(targets)} 行…")
        self.refresh_thread = threading.Thread(
            target=self._refresh_work_safe,
            args=(output_dir, targets),
            daemon=False,
        )
        self.refresh_thread.start()
        self.root.after(100, self._poll_refresh)

    def _refresh_work_safe(self, output_dir: Path, targets: list) -> None:
        """可取消、原子写回且补齐稳定 ID 的文档更新。"""
        logger = logging.getLogger("douyin_tool")
        updates: dict[int, dict] = {}
        failed_jobs: list[tuple[int | None, str]] = []
        try:
            for index, (seq, rec) in enumerate(targets, 1):
                ensure_not_cancelled(self.cancel_event)
                self._post(
                    "rprogress", {"i": index, "n": len(targets), "seq": seq}
                )
                if index > 1:
                    interruptible_wait(1 + random.random(), self.cancel_event)
                link = rec.get("raw_input") or ""
                fetched, fail_status = fetch_with_retry(
                    logger, seq, link, self.cancel_event
                )
                updates[seq] = {
                    "record": fetched,
                    "status": "正常" if fetched is not None else fail_status,
                    "link": link,
                }
                if fetched is None:
                    failed_jobs.append((seq, link))
                else:
                    logger.info(
                        "顺序 %d 更新成功，作品 ID %s，点赞 %d，评论 %d",
                        seq,
                        fetched.aweme_id,
                        fetched.fields["likes"],
                        fetched.fields["comments"],
                    )

            rows = exporter.read_records(output_dir / "提取记录.xlsx")
            changed_seqs: list[int] = []
            for seq, update in updates.items():
                rec = dict(rows.get(seq) or {})
                fetched = update["record"]
                if fetched is not None:
                    rec.update(
                        {
                            "raw_input": update["link"],
                            "title": fetched.fields["title"],
                            "tags": fetched.fields["tags"],
                            "likes": fetched.fields["likes"],
                            "comments": fetched.fields["comments"],
                            "author": fetched.fields["author"],
                            "status": "正常",
                            "aweme_id": fetched.aweme_id,
                            "work_kind": "图文" if fetched.kind == "note" else "视频",
                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    rows[seq] = rec
                    changed_seqs.append(seq)
                elif rec.get("likes") or rec.get("title") or rec.get("status") == "正常":
                    logger.warning(
                        "顺序 %d 更新失败（%s），保留原有正常数据",
                        seq,
                        update["status"],
                    )
                else:
                    rec["status"] = update["status"]
                    rows[seq] = rec
                    changed_seqs.append(seq)
            if changed_seqs:
                exporter.update_records(
                    output_dir / "提取记录.xlsx", rows, changed_seqs
                )
            ok_count = sum(1 for update in updates.values() if update["record"] is not None)
            self._post(
                "rdone",
                {
                    "ok": ok_count,
                    "fail": len(updates) - ok_count,
                    "total": len(updates),
                    "failed_jobs": failed_jobs,
                },
            )
        except TaskCancelled:
            logger.info("强制刷新已按用户请求取消")
            self._post(
                "rcancelled",
                {"processed": len(updates), "total": len(targets), "failed_jobs": failed_jobs},
            )
        except Exception as exc:
            logger.exception("强制刷新异常终止")
            self._post("rerror", str(exc))

    def _poll_refresh(self) -> None:
        try:
            while True:
                kind, payload, _extra = self.message_queue.get_nowait()
                if kind == "rprogress":
                    self.progress["value"] = int(payload["i"] / max(1, payload["n"]) * 100)
                    self.progress_label.config(
                        text=f"刷新 {payload['i']}/{payload['n']}（顺序 {payload['seq']}）"
                    )
                elif kind == "rdone":
                    was_automatic = self.auto_refresh
                    self.refreshing = False
                    self.auto_refresh = False
                    self.failed_jobs = list(payload.get("failed_jobs") or [])
                    self._set_task_controls(True)
                    self.update_button.config(text="强制刷新记录")
                    self.progress["value"] = 0
                    self.progress_label.config(text="")
                    self.load_existing_records()
                    self.status_var.set(
                        payload.get("message")
                        or (
                            ("启动自动刷新完成" if was_automatic else "强制刷新完成")
                            + f"：共 {payload['total']} 行"
                            f"（正常 {payload['ok']}，异常 {payload['fail']}）"
                        )
                    )
                    if self.close_requested:
                        self.root.after(50, self._wait_then_close)
                    return
                elif kind == "rcancelled":
                    self.refreshing = False
                    self.auto_refresh = False
                    self.failed_jobs = list(payload.get("failed_jobs") or [])
                    self._set_task_controls(True)
                    self.update_button.config(text="强制刷新记录")
                    self.progress["value"] = 0
                    self.progress_label.config(text="")
                    self.status_var.set(
                        f"更新已取消：已检查 {payload.get('processed', 0)}/{payload.get('total', 0)} 行"
                    )
                    if self.close_requested:
                        self.root.after(50, self._wait_then_close)
                    return
                elif kind == "rerror":
                    self.refreshing = False
                    self.auto_refresh = False
                    self._set_task_controls(True)
                    self.update_button.config(text="强制刷新记录")
                    self.progress["value"] = 0
                    self.progress_label.config(text="")
                    self.status_var.set(f"强制刷新失败：{payload}")
                    if self.close_requested:
                        self.root.after(50, self._wait_then_close)
                    return
        except queue.Empty:
            pass
        if self.refreshing:
            self.root.after(100, self._poll_refresh)

    def _schedule_renumber(self, _event=None) -> None:
        """输入变化后统一调度：锁定序号并维护末尾待填编号。"""
        if self._renumber_after_id is not None:
            self.root.after_cancel(self._renumber_after_id)
        self._renumber_after_id = self.root.after_idle(self._renumber_input)

    def _renumber_input(self) -> None:
        """锁定序号 + 追加下一个待填编号。

        - 序号永远等于条目位置：把 5. 改成 9. 也会恢复成 5.；
        - 只规范/恢复序号前缀，后面的原始链接与分享文案原样保留；
        - 多行粘贴仍视为一条内容，不逐行重排。
        """
        self._enforce_input_sequences()

    def clear_input(self) -> None:
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", "1.")
        self._style_input_sequences()
        self._save_input_cache()
        self.status_var.set("输入已清空")


def report_unhandled_error(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback,
    parent: tk.Misc | None = None,
) -> None:
    """无控制台运行时把未处理异常写入文件，并通过图形弹窗告知用户。"""
    details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    try:
        STARTUP_ERROR_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STARTUP_ERROR_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{details}")
    except Exception:
        pass
    try:
        messagebox.showerror(
            "抖音信息提取工具",
            f"程序遇到错误：{exc_value}\n\n详细信息已写入：\n{STARTUP_ERROR_FILE}",
            parent=parent,
        )
    except Exception:
        pass


def main() -> None:
    root: tk.Tk | None = None
    try:
        root = tk.Tk()
        root.report_callback_exception = lambda exc_type, exc_value, exc_traceback: (
            report_unhandled_error(exc_type, exc_value, exc_traceback, root)
        )
        DouyinExtractorApp(root)
        root.mainloop()
    except Exception:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        if exc_type is not None and exc_value is not None:
            report_unhandled_error(exc_type, exc_value, exc_traceback, root)


if __name__ == "__main__":
    main()
