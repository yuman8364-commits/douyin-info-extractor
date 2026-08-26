# -*- coding: utf-8 -*-
"""只读观察指定 Excel 第一条抖音链接在专用 Edge 中的实际跳转。"""

from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parents[1]))

import exporter
import extractor
from playwright.sync_api import sync_playwright


def ids_from_url(url: str) -> list[str]:
    parsed = urlparse(url)
    values = re.findall(r"/(?:video|note|slides)/(\d+)", parsed.path)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "item_id"):
        values.extend(query.get(key) or [])
    return list(dict.fromkeys(value for value in values if value.isdigit()))


def main() -> int:
    workbook = Path(sys.argv[1])
    profile = Path(sys.argv[2])
    record = exporter.read_records(workbook)[1]
    raw_input = record["raw_input"]
    if len(sys.argv) > 3 and sys.argv[3] == "--engine":
        access = extractor.AccessContext(
            profile.parent,
            verification_timeout=30,
            notice=lambda event, message: print(f"NOTICE={event}:{message}", flush=True),
        )
        try:
            access.fetch_record(raw_input)
            print("ENGINE_RESULT=UNEXPECTED_SUCCESS", flush=True)
            return 1
        except Exception as exc:
            print(f"ENGINE_RESULT={exc.__class__.__name__}:{exc}", flush=True)
            return 0 if isinstance(exc, extractor.TargetUnavailableError) else 1
        finally:
            access.close()
    with extractor._new_session() as session:
        final_url = extractor.resolve_share_url(session, extractor.extract_url(raw_input))
    _kind, target_id = extractor.parse_id_kind(final_url)
    target_url = f"https://www.douyin.com/video/{target_id}"
    print(f"TARGET_ID={target_id}", flush=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel="msedge", headless=False,
            viewport=None, locale="zh-CN"
        )
        page = context.pages[0] if context.pages else context.new_page()
        started = time.monotonic()
        page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
        last = None
        while time.monotonic() - started < 18:
            try:
                snapshot = (page.url, tuple(ids_from_url(page.url)), page.title())
                if snapshot != last:
                    elapsed = time.monotonic() - started
                    print(
                        f"T={elapsed:.1f} URL={snapshot[0]} IDS={snapshot[1]} TITLE={snapshot[2]}",
                        flush=True,
                    )
                    last = snapshot
                page.wait_for_timeout(250)
            except Exception as exc:
                print(f"PAGE_ERROR={exc.__class__.__name__}:{exc}", flush=True)
                break
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
