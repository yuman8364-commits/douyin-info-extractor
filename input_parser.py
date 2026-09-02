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


def existing_duplicate_urls(text: str, pasted_text: str) -> list[tuple[str, int]]:
    """返回粘贴内已在输入列表中存在的 ``(链接, 序号)``。

    只比较可确定的完整抖音链接，不联网猜测两个不同短链是否
    指向同一作品。
    """
    existing: dict[str, int] = {}
    real_seq = 0
    for block in split_entry_blocks(text):
        if _is_placeholder_block(block):
            continue
        real_seq += 1
        raw = _block_raw_content(block, real_seq)
        for url in extractor.extract_urls(raw):
            existing.setdefault(url, real_seq)

    duplicates: list[tuple[str, int]] = []
    for url in extractor.extract_urls(pasted_text):
        if url in existing:
            duplicates.append((url, existing[url]))
    return duplicates


def removed_urls(previous_text: str, current_text: str) -> list[str]:
    """返回从上一版输入中消失的链接，并保留原出现顺序。"""
    current_urls = set(extractor.extract_urls(current_text))
    return [
        url for url in extractor.extract_urls(previous_text) if url not in current_urls
    ]


def remove_entry_at_line(text: str, line_number: int) -> tuple[str, int | None, str]:
    """删除指定行所在的输入条目，并仅将其后的序号依次前移。

    光标在末尾待填编号或分隔线上时不删除任何条目，避免误删。
    """
    lines = (text or "").splitlines()
    if not lines:
        return "1.", None, ""
    target_line = max(0, min(int(line_number or 1) - 1, len(lines) - 1))
    if DIVIDER_RE.match(lines[target_line].strip()):
        return normalize_input_text(text), None, ""

    spans: list[tuple[int, int, list[str]]] = []
    start = 0
    for index, line in enumerate(lines):
        if not DIVIDER_RE.match(line.strip()):
            continue
        if start < index:
            spans.append((start, index, lines[start:index]))
        start = index + 1
    if start < len(lines):
        spans.append((start, len(lines), lines[start:]))

    kept: list[str] = []
    removed_seq: int | None = None
    removed_raw = ""
    real_seq = 0
    for span_start, span_end, block in spans:
        if not block or _is_placeholder_block(block):
            continue
        real_seq += 1
        raw = _block_raw_content(block, real_seq)
        if span_start <= target_line < span_end:
            removed_seq = real_seq
            removed_raw = raw
        elif raw:
            kept.append(raw)

    if removed_seq is None:
        return normalize_input_text(text), None, ""
    if not kept:
        return "1.", removed_seq, removed_raw
    return normalize_input_text(f"\n{DIVIDER}\n".join(kept)), removed_seq, removed_raw


def remove_matching_entry(text: str, seq: int, raw_input: str) -> tuple[str, int]:
    """删除与记录链接匹配的输入块并连续重排编号。

    有可识别链接时只按链接匹配，避免当前输入框内容与表格顺序不同步时
    误删同位置的其它任务；旧记录没有链接时才回退到序号位置。
    """
    raw_entries: list[str] = []
    for index, block in enumerate(split_entry_blocks(text), 1):
        if _is_placeholder_block(block):
            continue
        raw = _block_raw_content(block, index)
        if raw:
            raw_entries.append(raw)

    target_urls = set(extractor.extract_urls(str(raw_input or "")))
    kept: list[str] = []
    removed = 0
    for index, raw in enumerate(raw_entries, 1):
        urls = set(extractor.extract_urls(raw))
        matches = bool(target_urls and urls.intersection(target_urls))
        if not target_urls:
            matches = index == int(seq)
        if matches:
            removed += 1
        else:
            kept.append(raw)

    if removed == 0:
        return normalize_input_text(text), 0
    if not kept:
        return "1.", removed
    combined = f"\n{DIVIDER}\n".join(kept)
    return normalize_input_text(combined), removed


def build_input_tasks(text: str) -> tuple[list[str], int]:
    """兼容旧调用：只返回原始任务文本。"""
    jobs, ignored = build_input_jobs(text)
    return [raw for _seq, raw in jobs], ignored
