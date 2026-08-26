# -*- coding: utf-8 -*-
"""手工浏览器兜底验收：传入含公开抖音链接的文本文件路径。"""

from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parents[1]))

import extractor


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: manual_browser_smoke.py <input-text-file>")
        return 2
    text = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
    urls = extractor.extract_urls(text)
    if not urls:
        print("FAIL no public Douyin URL found")
        return 2

    profile = Path(tempfile.mkdtemp(prefix="douyin-browser-test-"))
    notices: list[tuple[str, str]] = []

    def notice(event: str, message: str) -> None:
        notices.append((event, message))
        print(f"{event}: {message}", flush=True)

    context = extractor.AccessContext(
        profile,
        notice=notice,
        verification_timeout=120,
    )
    try:
        records = [context.fetch_record(url) for url in urls[:2]]
        for record in records:
            print(f"SUCCESS {record.aweme_id} {record.fields['title'][:40]}", flush=True)
        print(
            "SUMMARY "
            f"records={len(records)} "
            f"verification_required={sum(1 for event, _ in notices if event == 'verification_required')}",
            flush=True,
        )
        return 0
    except Exception as exc:
        print(f"FAIL {exc.__class__.__name__}: {exc}", flush=True)
        return 1
    finally:
        context.close()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
