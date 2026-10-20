# -*- coding: utf-8 -*-
"""采集离线回归测试夹具（fixtures）：把书架各站的真实页面 HTML 存到本地。

用法：python scripts/build_fixtures.py
之后 pytest tests/ 会优先用本地 fixture 跑解析回归（零网络依赖）。

fixture 含小说正文，版权属原站点，已在 .gitignore 排除、不进仓库。
"""
import glob
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
for _p in (os.path.join(BASE, "autobs"), os.path.join(os.path.dirname(BASE), "autobs")):
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

from engine.fetcher import LayeredFetcher  # noqa: E402

FIXTURE_DIR = os.path.join(BASE, "tests", "fixtures")


def main():
    f = LayeredFetcher()
    made = 0
    for fp in glob.glob(os.path.join(BASE, "data", "books", "*.json")):
        b = json.load(open(fp, encoding="utf-8"))
        host = b.get("url", "").split("/")[2] if "://" in b.get("url", "") else ""
        if not host or not b.get("chapters"):
            continue
        tag = host.replace("www.", "").replace("m.", "").split(".")[0]
        outdir = os.path.join(FIXTURE_DIR, tag)
        os.makedirs(outdir, exist_ok=True)
        meta = {"book_url": b["url"], "title": b.get("title"),
                "chapter_count": b.get("chapter_count"),
                "expect_first": b["chapters"][0]["title"],
                "expect_last": b["chapters"][-1]["title"]}
        # 书页/目录页
        for name, url in [("book.html", b.get("list_url") or b["url"]),
                          ("ch_first.html", b["chapters"][0]["url"]),
                          ("ch_mid.html", b["chapters"][len(b["chapters"]) // 2]["url"]),
                          ("ch_last.html", b["chapters"][-1]["url"])]:
            r = f.get(url)
            if r is None or len(r.content) < 1000:
                print("[skip] %s %s 不可用" % (tag, name))
            else:
                open(os.path.join(outdir, name), "wb").write(r.content)
                made += 1
        json.dump(meta, open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("[ok] %s: %s 章" % (tag, b.get("chapter_count")))
    print("完成：%d 个页面文件 -> tests/fixtures/")


if __name__ == "__main__":
    main()
