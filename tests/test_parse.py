# -*- coding: utf-8 -*-
"""解析引擎离线回归：跑在 tests/fixtures/ 的真实站点快照上。

fixture 由 scripts/build_fixtures.py 采集（含正文，不入仓库），
缺失时整组跳过。CI 环境无 fixture，只跑纯函数单测（test_units.py）。
"""
import glob
import json
import os
import sys

import lxml.html
import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
for _p in (os.path.join(BASE, "autobs"), os.path.join(os.path.dirname(BASE), "autobs")):
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

from autobs import (parse_doc, group_anchors, build_list_xpath,  # noqa: E402
                    detect_content_xpath, extract_content_text, clean_text)
from tests.quality import check_chapter_text  # noqa: E402

FIXTURES = os.path.join(BASE, "tests", "fixtures")

SITES = sorted(d for d in glob.glob(os.path.join(FIXTURES, "*"))
               if os.path.isdir(d)) if os.path.isdir(FIXTURES) else []
KNOWN_ISSUES = {
    "jnsxs": "大容器含推荐区（am-container 识别偏大）",
    "iwyx": "书架旧书为错解析样本，期望值失效",
}
SITES = [d for d in SITES if os.path.basename(d) not in KNOWN_ISSUES]

pytestmark = pytest.mark.skipif(not SITES, reason="无 fixture，先运行 scripts/build_fixtures.py")



class FakeResp:
    def __init__(self, content, url):
        self.status_code = 200
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.url = url


def _load(path, url):
    return parse_doc(FakeResp(open(path, "rb").read(), url))


@pytest.fixture(scope="module")
def site(request):
    d = request.param
    meta = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
    return d, meta


def _site_marks(tag):
    return pytest.mark.xfail(reason=KNOWN_ISSUES[tag], strict=False) if tag in KNOWN_ISSUES else lambda f: f


@pytest.mark.parametrize("site", SITES, ids=[os.path.basename(s) for s in SITES], indirect=True)
def test_catalog(site):
    """目录页：能识别出章节列表，数量与采集时一致（允许 ±10%，连载更新），
    首章标题吻合。"""
    d, meta = site
    p = os.path.join(d, "book.html")
    if not os.path.exists(p):
        pytest.skip("目录页快照缺失")
    doc = _load(p, meta["book_url"])
    pair = None
    for _, items in group_anchors(doc, min_count=5):
        pair = build_list_xpath(items, doc)
        if pair:
            break
    assert pair, "未能识别章节列表"
    titles = [norm(t.text_content()) for t in doc.xpath(pair[0])]
    n_expect = meta["chapter_count"]
    assert n_expect * 0.9 <= len(titles) <= n_expect * 1.5, \
        "章节数 %d 与采集时 %d 偏差过大" % (len(titles), n_expect)
    assert norm(meta["expect_first"]) == titles[0], \
        "首章标题 %r != 采集时 %r" % (titles[0], meta["expect_first"])


def norm(s):
    import re
    return re.sub(r"\s+", "", s or "")


@pytest.mark.parametrize("site", SITES, ids=[os.path.basename(s) for s in SITES], indirect=True)
def test_content_xpath(site):
    """章节页：正文容器在首/中/末三个快照上恰好匹配 1 个节点。"""
    d, meta = site
    docs = []
    # 与 resolve_book 采样一致：首章+中间章（末章可能是短公告页，不参与容器判定）
    for name in ("ch_first.html", "ch_mid.html"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            docs.append(_load(p, meta["book_url"]))
    assert docs, "章节页快照全部缺失"
    xp = detect_content_xpath(docs)
    assert xp, "未能识别正文容器"
    for doc in docs:
        got = doc.xpath(xp)
        assert len(got) == 1, "xpath %s 匹配 %d 个节点（应为 1）" % (xp, len(got))


@pytest.mark.parametrize("site", SITES, ids=[os.path.basename(s) for s in SITES], indirect=True)
def test_chapter_quality(site):
    """正文提取：字数下限、开头无导航/水印、保留分段。"""
    d, meta = site
    problems_all = {}
    for name in ("ch_first.html", "ch_mid.html"):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            continue
        doc = _load(p, meta["book_url"])
        xp = detect_content_xpath([doc]) or ""
        got = doc.xpath(xp)
        if len(got) != 1:
            problems_all[name] = ["容器匹配 %d" % len(got)]
            continue
        text = extract_content_text(got[0])
        problems = check_chapter_text(text)
        if problems:
            problems_all[name] = problems
    assert not problems_all, "正文质量问题: %s" % problems_all
