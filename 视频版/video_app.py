# -*- coding: utf-8 -*-
"""抖音视频批量提取工具：只下载视频文件，不导出作品信息或其他媒体。"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import random
import re
import shutil
import sys
import tempfile
import threading
from tkinter import filedialog, messagebox, ttk
import tkinter as tk


SOURCE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SOURCE_DIR.parent
if not getattr(sys, "frozen", False) and str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import extractor  # noqa: E402
import input_parser  # noqa: E402
from tasking import TaskCancelled, ensure_not_cancelled, interruptible_wait  # noqa: E402
from video_history import VideoHistory, aweme_identity, identities_for_input  # noqa: E402


APP_VERSION = "1.1.0"
APP_TITLE = "抖音视频批量提取工具"
LOG_NAME = "运行日志.log"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR


def _writable_state_dir() -> Path:
    """优先把程序状态放在发布目录，目录不可写时回退到本机状态目录。"""
    candidates = [APP_DIR / "data"]
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
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8").lstrip("\ufeff"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(updates: dict) -> None:
    try:
        config = load_config()
        config.update(updates)
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).exception("保存程序配置失败")


def load_input_cache() -> str:
    try:
        return INPUT_CACHE_FILE.read_text(encoding="utf-8").lstrip("\ufeff").rstrip("\n")
    except OSError:
        return ""


def default_output_dir() -> str:
    desktop = Path.home() / "Desktop"
    if desktop.exists():
        return str(desktop / "抖音视频批量提取")
    return str(APP_DIR / "output")


def setup_logger(log_path: Path) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    return logging.getLogger("douyin_video_batch")


def fetch_with_retry(
    logger: logging.Logger,
    seq: int,
    link: str,
    cancel_event: threading.Event,
    access_context: extractor.AccessContext,
) -> tuple[extractor.FetchedRecord | None, str | None]:
    """对可恢复的作品页读取失败有限重试；验证码交给整批暂停逻辑。"""
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


def _next_free_video_path(output_dir: Path, seq: int) -> Path:
    """用编号生成文件名；同名文件已存在时递增后缀，绝不覆盖。"""
    base = max(1, int(seq))
    candidate = output_dir / f"{base}.mp4"
    suffix = 2
    while candidate.exists():
        candidate = output_dir / f"{base}_{suffix}.mp4"
        suffix += 1
    return candidate


def _copy_existing_video(source: Path, target: Path) -> Path:
    """把本机已提取的视频原子复制到当前选定目录，不访问网络。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent
    )
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
        return target
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class VideoBatchDownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x700")
        self.root.minsize(940, 560)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.message_queue: queue.Queue[tuple[str, object, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.running = False
        self.close_requested = False
        self.success_count = 0
        self.skipped_count = 0
        self.failure_count = 0
        self.unchecked_count = 0
        self.batch_total = 0
        self.paused_message = ""
        self.logger = logging.getLogger("douyin_video_batch")
        self.video_history: VideoHistory | None = None

        config = load_config()
        self.output_dir = Path(config.get("output_dir") or default_output_dir())
        self._build_ui()
        cached = load_input_cache()
        if cached.strip():
            self.input_text.insert("1.0", cached)
        self.input_text.mark_set("insert", "end-1c")
        self.input_text.see("insert")

    def _build_ui(self) -> None:
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="选择视频保存目录…", command=self.browse_output)
        file_menu.add_command(label="打开视频保存目录", command=self.open_output)
        file_menu.add_command(label="打开运行日志", command=self.open_log)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menu_bar.add_cascade(label="文件", menu=file_menu)
        self.root.config(menu=menu_bar)

        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")
        ttk.Label(header, text="只批量下载抖音视频；不保存文案、封面、图集或作品信息").pack(side="left")
        ttk.Label(header, text=f"版本 {APP_VERSION}").pack(side="right")

        input_header = ttk.Frame(main)
        input_header.pack(fill="x", pady=(12, 4))
        ttk.Label(input_header, text="粘贴作品链接或分享文案，支持一次粘贴多条：").pack(side="left")
        self.clear_button = ttk.Button(input_header, text="清空输入", command=self.clear_input)
        self.clear_button.pack(side="right")

        input_frame = ttk.Frame(main)
        input_frame.pack(fill="x")
        self.input_text = tk.Text(input_frame, height=8, wrap="word", font=("Microsoft YaHei UI", 10), undo=True)
        input_scroll = ttk.Scrollbar(input_frame, orient="vertical", command=self.input_text.yview)
        self.input_text.configure(yscrollcommand=input_scroll.set)
        self.input_text.pack(side="left", fill="both", expand=True)
        input_scroll.pack(side="right", fill="y")
        # 剪贴板只在用户明确粘贴时读取，不做后台轮询。
        self.input_text.bind("<<Paste>>", self._on_input_paste)

        output_frame = ttk.Frame(main)
        output_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(output_frame, text="视频保存目录:").pack(side="left")
        self.output_var = tk.StringVar(value=str(self.output_dir))
        self.output_entry = ttk.Entry(output_frame, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.output_browse_button = ttk.Button(output_frame, text="浏览…", command=self.browse_output)
        self.output_browse_button.pack(side="right")

        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(10, 8))
        self.start_button = ttk.Button(controls, text="开始批量提取", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止任务", command=self.stop_task, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(controls, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 8))
        self.progress_label = ttk.Label(controls, text="")
        self.progress_label.pack(side="right")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var).pack(fill="x", pady=(0, 6))

        result_frame = ttk.LabelFrame(main, text="本次任务", padding=6)
        result_frame.pack(fill="both", expand=True)
        columns = ("seq", "link", "file", "status")
        self.tree = ttk.Treeview(result_frame, columns=columns, show="headings")
        headings = {
            "seq": ("序号", 62),
            "link": ("作品链接", 480),
            "file": ("保存文件", 300),
            "status": ("状态", 220),
        }
        for column in columns:
            title, width = headings[column]
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=55, anchor="w" if column != "seq" else "center")
        self.tree.pack(side="left", fill="both", expand=True)
        result_scroll = ttk.Scrollbar(result_frame, orient="vertical", command=self.tree.yview)
        result_scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=result_scroll.set)

    def _post(self, kind: str, payload=None, extra: str = "") -> None:
        self.message_queue.put((kind, payload, extra))

    def _save_input_cache(self) -> None:
        try:
            content = self.input_text.get("1.0", "end-1c")
            INPUT_CACHE_FILE.write_text(content + "\n", encoding="utf-8")
        except OSError:
            self.logger.exception("保存输入缓存失败")

    @staticmethod
    def _duplicate_urls_against_known(current: str, candidate: str) -> list[str]:
        known = {
            input_parser.link_identity(url)
            for url in extractor.extract_urls(current)
            if input_parser.link_identity(url)
        }
        duplicates: list[str] = []
        for url in extractor.extract_urls(candidate):
            identity = input_parser.link_identity(url)
            if identity in known:
                duplicates.append(url)
            else:
                known.add(identity)
        return duplicates

    @staticmethod
    def _remove_duplicate_urls_from_paste(candidate: str, duplicate_urls: list[str]) -> str:
        duplicate_set = set(duplicate_urls)
        kept_lines: list[str] = []
        for line in candidate.splitlines():
            hits = [url for url in extractor.extract_urls(line) if url in duplicate_set]
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
        try:
            pasted = str(self.root.clipboard_get())
        except tk.TclError:
            return None
        current = self.input_text.get("1.0", "end-1c")
        try:
            current = self.input_text.get("1.0", "sel.first") + self.input_text.get("sel.last", "end-1c")
        except tk.TclError:
            pass
        duplicates = self._duplicate_urls_against_known(current, pasted)
        if not duplicates:
            return None
        filtered = self._remove_duplicate_urls_from_paste(pasted, duplicates)
        try:
            self.input_text.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        if filtered:
            self.input_text.insert("insert", filtered)
        self.status_var.set(f"已过滤本次粘贴中的 {len(duplicates)} 条重复链接，其余链接已保留")
        return "break"

    def _set_task_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (self.start_button, self.clear_button, self.output_browse_button):
            widget.config(state=state)
        self.input_text.config(state=state)
        self.output_entry.config(state=state)
        self.stop_button.config(state="disabled" if enabled else "normal")

    def browse_output(self) -> None:
        if self.running:
            return
        chosen = filedialog.askdirectory(
            title="选择视频保存目录",
            initialdir=self.output_var.get().strip() or default_output_dir(),
        )
        if chosen:
            self.output_var.set(chosen)
            self.output_dir = Path(chosen)
            save_config({"output_dir": str(self.output_dir)})

    def open_output(self) -> None:
        path = Path(self.output_var.get().strip() or default_output_dir())
        path.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(path)

    def open_log(self) -> None:
        path = STATE_DIR / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("（暂无日志，开始批量提取后自动生成）\n", encoding="utf-8")
        if hasattr(os, "startfile"):
            os.startfile(path)

    def clear_input(self) -> None:
        if self.running:
            return
        self.input_text.delete("1.0", "end")
        self._save_input_cache()
        self.status_var.set("输入已清空")

    def start(self) -> None:
        if self.running:
            return
        input_text = self.input_text.get("1.0", "end")
        jobs, ignored = input_parser.build_input_jobs(input_text)
        if not jobs:
            self.status_var.set("请先粘贴至少一条抖音作品链接")
            return
        output_dir = Path(self.output_var.get().strip() or default_output_dir())
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status_var.set(f"视频保存目录不可用：{exc}")
            return

        self._save_input_cache()
        self.output_dir = output_dir
        save_config({"output_dir": str(output_dir)})
        self.logger = setup_logger(STATE_DIR / LOG_NAME)
        self.video_history = VideoHistory(STATE_DIR, output_dir, self.logger)
        self.logger.info("开始视频批量提取：%d 条，保存目录 %s", len(jobs), output_dir)
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.running = True
        self.close_requested = False
        self.cancel_event.clear()
        self.success_count = 0
        self.skipped_count = 0
        self.failure_count = 0
        self.unchecked_count = 0
        self.batch_total = len(jobs)
        self.paused_message = ""
        self._set_task_controls(False)
        self.start_button.config(text="提取中…")
        ignored_note = f"，忽略 {ignored} 行无链接内容" if ignored else ""
        self.status_var.set(f"开始处理 {len(jobs)} 条链接{ignored_note}…")
        self.progress["value"] = 0
        self.worker = threading.Thread(target=self._work_safe, args=(jobs, output_dir), daemon=False)
        self.worker.start()
        self.root.after(100, self._poll)

    def stop_task(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self.stop_button.config(state="disabled")
        self.status_var.set("正在停止任务；已完成的视频会保留…")

    def _access_context(self) -> extractor.AccessContext:
        return extractor.AccessContext(
            BROWSER_PROFILE_DIR,
            self.cancel_event,
            lambda event, message: self._post("verification", {"event": event}, message),
        )

    def _report_reused_video(
        self,
        seq: int,
        link: str,
        existing_path: Path,
        identities: list[str],
        output_dir: Path,
        aweme_id: str | None = None,
    ) -> None:
        """报告已存在视频；必要时从本机旧目录复制，始终不重新下载。"""
        try:
            same_directory = existing_path.resolve().parent == output_dir.resolve()
            if same_directory:
                saved_path = existing_path
                status = "已提取，跳过重新下载"
            else:
                saved_path = _copy_existing_video(
                    existing_path, _next_free_video_path(output_dir, seq)
                )
                status = "本机已有，已复制到所选目录（未重新下载）"
            history_keys = [*identities]
            if aweme_id:
                history_keys.append(aweme_identity(aweme_id))
            if self.video_history is not None:
                self.video_history.record(history_keys, saved_path)
            self.skipped_count += 1
            self.logger.info(
                "顺序 %d 命中已提取记录，跳过网络下载：%s", seq, saved_path
            )
            self._post(
                "result",
                {"seq": seq, "link": link, "file": saved_path.name, "status": status},
            )
        except Exception as exc:
            self.failure_count += 1
            self.logger.exception("顺序 %d 找到本机旧视频，但处理失败", seq)
            self._post(
                "result",
                {
                    "seq": seq,
                    "link": link,
                    "file": existing_path.name,
                    "status": f"已找到本机视频，但复制到所选目录失败：{exc}",
                },
            )

    def _work_safe(self, jobs: list[tuple[int | None, str]], output_dir: Path) -> None:
        context = self._access_context()
        total = len(jobs)
        completed = 0
        current_index = 0
        try:
            for index, current_job in enumerate(jobs, 1):
                current_index = index
                ensure_not_cancelled(self.cancel_event)
                input_seq, line = current_job
                seq = int(input_seq) if input_seq is not None else index
                if index > 1:
                    interruptible_wait(0.5 + random.random() * 0.5, self.cancel_event)
                self.logger.info("[%d/%d] 开始处理顺序 %d：%s", index, total, seq, line[:120])
                try:
                    identities = identities_for_input(line)
                    history = self.video_history
                    existing_path = history.find(identities) if history is not None else None
                    if existing_path is not None:
                        self._report_reused_video(
                            seq, line, existing_path, identities, output_dir
                        )
                    else:
                        fetched, error = fetch_with_retry(self.logger, seq, line, self.cancel_event, context)
                        if fetched is None:
                            self.failure_count += 1
                            self._post("result", {"seq": seq, "link": line, "file": "", "status": error or "获取失败"})
                        elif fetched.kind != "video":
                            self.failure_count += 1
                            self._post("result", {"seq": seq, "link": line, "file": "", "status": "图文作品不支持；仅下载视频"})
                            self.logger.info("顺序 %d 是图文作品，已跳过", seq)
                        else:
                            work_identity = aweme_identity(fetched.aweme_id)
                            existing_path = history.find([work_identity]) if history is not None else None
                            if existing_path is not None:
                                self._report_reused_video(
                                    seq,
                                    line,
                                    existing_path,
                                    identities,
                                    output_dir,
                                    fetched.aweme_id,
                                )
                            else:
                                target = _next_free_video_path(output_dir, seq)
                                last_progress = [0]

                                def video_progress(done: int, size: int, n=index, number=seq) -> None:
                                    if done != 0 and done - last_progress[0] < 2 * 1024 * 1024 and done != size:
                                        return
                                    last_progress[0] = done
                                    self._post("bytes", {"index": n, "seq": number, "done": done, "total": size})

                                video_progress(0, 0)
                                saved_path, size_hit = extractor.download_video(
                                    fetched.session,
                                    fetched.item,
                                    target,
                                    video_progress,
                                    cancel_event=self.cancel_event,
                                    browser_context=context.browser_context,
                                    browser_context_provider=context.ensure_browser_context,
                                )
                                history_keys = [*identities, work_identity]
                                history_saved = history.record(history_keys, saved_path) if history is not None else False
                                status = "完成（已有同大小视频，仅作提醒）" if size_hit else "完成"
                                if not history_saved:
                                    status += "（本机去重记录写入失败）"
                                self.success_count += 1
                                self._post("result", {"seq": seq, "link": line, "file": saved_path.name, "status": status})
                                self.logger.info("顺序 %d 视频已保存：%s", seq, saved_path)
                except TaskCancelled:
                    raise
                except extractor.BrowserVerificationError as exc:
                    self.unchecked_count = total - index + 1
                    self.paused_message = f"批次已暂停：{exc}"
                    self._post("paused", {"unchecked": self.unchecked_count}, self.paused_message)
                    break
                except Exception as exc:
                    self.failure_count += 1
                    self.logger.exception("顺序 %d 下载失败", seq)
                    self._post("result", {"seq": seq, "link": line, "file": "", "status": f"下载失败：{exc}"})
                completed = index
                self._post("progress", {"done": completed, "total": total})
        except TaskCancelled:
            self.logger.info("视频批量任务已按用户请求取消")
            self._post("cancelled")
        except Exception as exc:
            self.logger.exception("视频批量任务异常终止")
            self._post("fatal", None, f"任务异常终止：{exc}")
        finally:
            context.close()
            if self.unchecked_count and current_index:
                self._post("progress", {"done": max(0, current_index - 1), "total": total})
            self._post("done")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload, extra = self.message_queue.get_nowait()
                if kind == "verification":
                    self.status_var.set(extra or "请在弹出的应用专用浏览器中完成验证")
                    self.progress_label.config(text="等待浏览器验证…")
                elif kind == "bytes":
                    data = payload or {}
                    done, size = int(data.get("done") or 0), int(data.get("total") or 0)
                    fraction = min(1.0, done / size) if size > 0 else 0.0
                    index = int(data.get("index") or 1)
                    total_jobs = max(1, self.batch_total)
                    self.progress["value"] = min(100, int((index - 1 + fraction) / total_jobs * 100))
                    detail = f"{done / 1048576:.1f}/{size / 1048576:.1f} MB" if size else "读取视频中"
                    self.progress_label.config(text=f"第 {index}/{total_jobs} 条 · {detail}")
                    self.status_var.set(f"顺序 {data.get('seq')} 视频下载中…")
                elif kind == "progress":
                    total = int((payload or {}).get("total") or 0)
                    done = int((payload or {}).get("done") or 0)
                    percent = int(done / total * 100) if total else 0
                    self.progress["value"] = percent
                    self.progress_label.config(text=f"批次 {done}/{total}")
                elif kind == "result":
                    data = payload or {}
                    self.tree.insert("", "end", values=(data.get("seq", ""), data.get("link", ""), data.get("file", ""), data.get("status", "")))
                    self.tree.yview_moveto(1.0)
                    if data.get("file"):
                        self.status_var.set(f"顺序 {data.get('seq')}：{data.get('status')}（{data.get('file')}）")
                elif kind == "paused":
                    self.unchecked_count = int((payload or {}).get("unchecked") or 0)
                    self.paused_message = extra or "批次已暂停"
                    self.status_var.set(self.paused_message)
                elif kind == "cancelled":
                    self.status_var.set("任务已取消，已完成的视频保留")
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
        self.start_button.config(text="开始批量提取")
        summary = f"本批完成：新下载 {self.success_count} 条，已提取跳过 {self.skipped_count} 条，失败 {self.failure_count} 条"
        if self.unchecked_count:
            summary += f"，未完成 {self.unchecked_count} 条"
        if self.paused_message:
            summary += f"；{self.paused_message}"
        self.status_var.set(summary)
        self.progress["value"] = 0
        self.progress_label.config(text="")
        if self.close_requested:
            self.root.after(50, self._wait_then_close)

    def _on_close(self) -> None:
        self._save_input_cache()
        if self.running or (self.worker is not None and self.worker.is_alive()):
            if not self.close_requested:
                if not messagebox.askyesno("停止任务并退出", "当前任务仍在运行。是否停止任务并退出？", parent=self.root):
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
    VideoBatchDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
