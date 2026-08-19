# -*- coding: utf-8 -*-
"""输入框条目解析与序号规范化。

本模块不依赖 Tkinter，便于对粘贴、编号和单次提取规则做独立测试。
"""

from __future__ import annotations

import re

import extractor

DIVIDER = "-" * 12
DIVIDER_RE = re.compile(r"^[-─—=]{6,}$")
BARE_NUMBER_RE = re.compile(r"^\d+\.\s*$")
ENTRY_PREFIX_RE = re.compile(r"^(\d+)[.、]\s*(.*)$")
PLAIN_PREFIX_RE = re.compile(r"^\d+[.、]")
PREFIX_LINE_RE = PLAIN_PREFIX_RE


def split_entry_blocks(text: str) -> list[list[str]]:
    """把输入框内容切成条目块。"""
    lines = (text or "").splitlines()
    has_divider = any(DIVIDER_RE.match(line.strip()) for line in lines)
    blocks: list[list[str]] = []
    current: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if DIVIDER_RE.match(line):
            if current:
                blocks.append(current)
                current = []
            continue
        if not line:
            if current:
                current.append("")
            continue
        if not has_divider and current:
            prefix_match = ENTRY_PREFIX_RE.match(line)
            if prefix_match and int(prefix_match.group(1)) == len(blocks) + 2:
                blocks.append(current)
                current = []
        current.append(raw_line)
    if current:
        blocks.append(current)
    return blocks


def _block_raw_content(block: list[str], expected_seq: int) -> str:
    """取条目块去掉界面序号后的原始内容。"""
    first = block[0].strip()
    match = ENTRY_PREFIX_RE.match(first)
    if match:
        prefix_ok = int(match.group(1)) == expected_seq
        has_separator = bool(re.match(r"^\d+[.、]\s+", first))
        if prefix_ok or has_separator:
            lines = ([match.group(2).strip()] if match.group(2).strip() else [])
        else:
            lines = [first]
    else:
        lines = [first]
    lines.extend(line.strip() for line in block[1:] if line.strip())
    return "\n".join(lines).strip()


def _is_placeholder_block(block: list[str]) -> bool:
    """判断是否为结尾的待填编号块。"""
    if len(block) != 1:
        return False
    text = block[0].strip()
    if not text or extractor.extract_urls(text):
        return False
    return re.fullmatch(r"\d+[.、]?", text) is not None


def build_input_jobs(text: str) -> tuple[list[tuple[int | None, str]], int]:
    """返回 ``(序号, 原始内容)`` 任务列表和忽略行数。"""
    jobs: list[tuple[int | None, str]] = []
    ignored = 0
    for seq, block in enumerate(split_entry_blocks(text), 1):
        if _is_placeholder_block(block):
            if re.fullmatch(r"\d+", block[0].strip()):
                ignored += 1
            continue
        raw = _block_raw_content(block, seq)
        if not raw:
            continue
        urls = extractor.extract_urls(raw)
        if len(urls) == 1:
            jobs.append((seq, raw))
        elif urls:
            jobs.extend((None, url) for url in urls)
        else:
            first = block[0].strip()
            if ENTRY_PREFIX_RE.match(first):
                jobs.append((seq, raw))
            else:
                ignored += 1
    return jobs, ignored


def input_job_at_line(text: str, line_number: int) -> tuple[int, str] | None:
    """返回光标所在条目；末尾占位编号回退到最近有效链接。"""
    lines = (text or "").splitlines()
    if not lines:
        return None
    original_index = max(0, min(int(line_number or 1) - 1, len(lines) - 1))
    checked_spans: set[tuple[int, int]] = set()

    for anchor in range(original_index, -1, -1):
        if DIVIDER_RE.match(lines[anchor].strip()):
            continue
        start = anchor
        while start > 0 and not DIVIDER_RE.match(lines[start - 1].strip()):
            start -= 1
        end = anchor + 1
        while end < len(lines) and not DIVIDER_RE.match(lines[end].strip()):
            end += 1
        span = (start, end)
        if span in checked_spans:
            continue
        checked_spans.add(span)

        block = lines[start:end]
        if not block or _is_placeholder_block(block):
            continue
        seq = 1 + sum(1 for line in lines[:start] if DIVIDER_RE.match(line.strip()))
        raw = _block_raw_content(block, seq)
        urls = extractor.extract_urls(raw)
        if not urls:
            continue
        if len(urls) == 1:
            return seq, raw
        cursor_urls = extractor.extract_urls(lines[original_index])
        return seq, (cursor_urls[0] if cursor_urls else urls[0])
    return None


def normalize_input_text(text: str) -> str:
    """按条目位置锁定序号，并只保留一个末尾待填编号。"""
    blocks = split_entry_blocks(text)
    real_blocks: list[str] = []
    for seq, block in enumerate(blocks, 1):
        if _is_placeholder_block(block):
            continue
        raw = _block_raw_content(block, seq)
        if raw:
            real_blocks.append(raw)

    if not real_blocks:
        return "1."

    lines: list[str] = []
    for index, raw in enumerate(real_blocks, 1):
        lines.append(f"{index}. {raw}")
        if index < len(real_blocks):
            lines.append(DIVIDER)
    lines.append(DIVIDER)
    lines.append(f"{len(real_blocks) + 1}.")
    return "\n".join(lines)


def links_from_records_in_sequence(records: dict[int, dict]) -> list[str]:
    """从表格记录中按「顺序」提取纯链接，保留每行链接的原始先后。"""
    links: list[str] = []
    for seq in sorted(records):
        record = records.get(seq) or {}
        links.extend(extractor.extract_urls(str(record.get("raw_input") or "")))
    return links


def format_ordered_links(links: list[str]) -> str:
    """把有序纯链接格式化为输入框的锁定编号布局。"""
    valid_links: list[str] = []
    for value in links:
        valid_links.extend(extractor.extract_urls(str(value or "")))
    if not valid_links:
        return "1."

    lines: list[str] = []
    for index, url in enumerate(valid_links, 1):
        lines.append(f"{index}. {url}")
        lines.append(DIVIDER)
    lines.append(f"{len(valid_links) + 1}.")
    return "\n".join(lines)


def build_input_tasks(text: str) -> tuple[list[str], int]:
    """兼容旧调用：只返回原始任务文本。"""
    jobs, ignored = build_input_jobs(text)
    return [raw for _seq, raw in jobs], ignored
