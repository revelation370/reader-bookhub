# -*- coding: utf-8 -*-
"""纯函数单测（无网络、无 fixture，CI 可跑）。
每个用例对应一个历史事故。"""
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
for _p in (os.path.join(BASE, "autobs"), os.path.join(os.path.dirname(BASE), "autobs")):
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

from autobs import (is_watermark_line, is_waf_page, SEO_TAIL,  # noqa: E402
                    CHAPTERISH_HEAD, NUMLIKE_TITLE, _cut_nav_prefix,
                    drop_latest_block, _is_js_text, extract_content_text,
                    garbled_ratio)
from engine.gateway import _url_hash  # noqa: E402
from engine.websearch import _score_url, _norm_title  # noqa: E402
from engine.search import _relevance  # noqa: E402
from engine.gateway import _filter_domain_watermark  # noqa: E402


# ---- is_watermark_line：水印库 ----
@pytest.mark.parametrize("line,killed", [
    ("👉👉 当前浏览器转码失败：请退出阅读模式", True),
    ("❚本书作者某作者提醒您《测试长书名》最新章节", True),
    ("如章节缺失请退#出#阅#读#模#式", True),
    ("本章未完，请点击下一页继续阅读！", True),
    ("上一章 章节目录 保存书签 下一章", True),          # yqyp 导航行
    ("[全本小说]:qbxs. N e t 一秒记住！", True),
    ("示例站>测试长书名>手机访问", True),        # 面包屑头
    ("◑喜欢看本书吗？那就记住 玩文学小说*网 的域名ｗａｎwx.com◑", True),  # wanwx 防盗水印
    ("请来玩文学小说*网*看最新章节*完整章节", True),
    ("你的朋友正在书荒，快去帮帮他吧", True),
    ("他推开门，走了进去。", False),                    # 正常正文
    ("“下一章是什么？”她问道。", False),                # 正文对话提及导航词（单处）
])
def test_watermark(line, killed):
    assert is_watermark_line(line) == killed


def test_watermark_long_line_protected():
    """超长行必是正文（防分段失效时全文被水印规则杀光）。"""
    long_line = "一秒记住" + "他慢慢走过长廊。" * 60
    assert not is_watermark_line(long_line)


# ---- _cut_nav_prefix：导航与正文同行 ----
def test_cut_nav_mixed_line():
    line = "手机访问番外5你是我的（第1页）上一章章节目录保存书签下一章“小黎，你哥来接你啦。”隔壁张姐的座位在他旁边。"
    out = _cut_nav_prefix(line)
    assert out.startswith("“小黎")


def test_cut_nav_keeps_normal():
    line = "“下一章是什么？”她抬起头问道，眼睛里带着光。"
    assert _cut_nav_prefix(line) == line  # 仅 1 处导航词，不动


# ---- is_waf_page：JS cookie 验证墙 ----
def test_js_cookie_wall():
    wall = (b'<!DOCTYPE html><html><head><title></title><script>'
            b'document.cookie = "ge_js_validator_20=x; path=/;";'
            b'window.location.reload();</script></head><body></body></html>')
    assert is_waf_page(wall)


def test_normal_page_not_waf():
    body = "<html><body><h1>第1章</h1><div id='content'>" + "正文" * 500 + \
           "</div></body></html>"
    assert not is_waf_page(body.encode("utf-8"))


# ---- 分类正则 ----
@pytest.mark.parametrize("t,ok", [
    ("测试长书名了免费阅读笔趣阁", True),
    ("第55章（1 / 2）", False),
])
def test_seo_tail(t, ok):
    assert bool(SEO_TAIL.search(t)) == ok


@pytest.mark.parametrize("t,ok", [
    ("第12章 借钱", True), ("番外5 你是我的", True), ("楔子", True),
    ("Chapter 3", True), ("1234、大结局", True),
    ("测试长书名是金主文吗", False),
])
def test_chapterish(t, ok):
    assert bool(CHAPTERISH_HEAD.match(t)) == ok


# ---- _score_url：书页 vs 章节页 ----
@pytest.mark.parametrize("url,is_chapter", [
    ("https://www.qbxs.net/book/114633/", False),
    ("https://www.jnsxs.org/infos/12275/", False),
    ("https://www.qbxs.net/read/114633/54220093.html", True),
    ("https://www.qbxs.net/read/114633/54220093_2.html", True),
    ("https://yuzhaiwu3.cc/novel/65671/12344772.html", True),   # /novel/数字/数字.html 章节页
    ("https://www.yqyp.net/book/8543171_1.html", False),        # 目录分页页（数字_数字 但单层）是书页
    ("https://www.iwyx.net/book/1012905.html", False),
])
def test_score_url(url, is_chapter):
    assert (_score_url(url) < 0) == is_chapter


# ---- drop_latest_block：顶部「最新章节」倒序小部件 ----
def _mkch(n):
    return [{"title": "第%d章" % i, "url": "https://t.example/b/%d.html" % i}
            for i in range(1, n + 1)]


def test_drop_latest_block_mirror():
    """历史事故：站目录页顶部最新8章倒序 + 完整正序列表被一起抓到。"""
    full = _mkch(10)
    latest_rev = list(reversed(full[-3:]))     # 第10、9、8 章倒序在前
    items = latest_rev + full
    out, dropped = drop_latest_block(items)
    assert dropped == 3
    assert [c["url"] for c in out] == [c["url"] for c in full]
    assert out[0]["title"] == "第1章"


def test_drop_latest_block_no_dup():
    out, dropped = drop_latest_block(_mkch(10))
    assert dropped == 0 and len(out) == 10


def test_drop_latest_block_not_mirror():
    """前缀 URL 不在正序块中重现（真独立内容）时不能误删。"""
    full = _mkch(10)
    other = [{"title": "推荐%d" % i, "url": "https://t.example/x/%d.html" % i}
             for i in range(3)]
    out, dropped = drop_latest_block(other + full)
    assert dropped == 0 and len(out) == 13


def test_drop_latest_block_triple():
    """同一 URL 出现 3 次（顶栏 + 侧栏 + 正序列表）→ 全部早于正序块的都删。"""
    full = _mkch(10)
    items = list(reversed(full[-2:])) + list(reversed(full[-2:])) + full
    out, dropped = drop_latest_block(items)
    assert dropped == 4
    assert [c["url"] for c in out] == [c["url"] for c in full]


def test_drop_latest_block_crosspage():
    """历史事故：boshishuwu 侧栏预告分页后段章节，本页 URL 不重现——
    靠「前缀章号递减 + 尾部递增」识别（跨页镜像）。"""
    tail = _mkch(60)                       # 第1..60章 正序
    preview = [{"title": "第%d章" % i, "url": "https://t.example/b/%d.html" % i}
               for i in range(70, 61, -1)]  # 第70..61章 倒序预览
    out, dropped = drop_latest_block(preview + tail)
    assert dropped == 9
    assert out[0]["title"] == "第1章" and len(out) == 60


def test_drop_latest_block_no_num_not_dropped():
    """无章号标题（正文序号缺失）不能被形态2误删。"""
    items = [{"title": "悄悄话 %d" % i, "url": "https://t.example/x/%d" % i}
             for i in range(30)]
    out, dropped = drop_latest_block(items)
    assert dropped == 0 and len(out) == 30


# ---- _is_js_text：反爬混淆的裸 JS 文本节点 ----
@pytest.mark.parametrize("line,js", [
    ('let urls=["hUUtUUtUUs".split("UU").join("")];window.__rr__hld=1;', True),
    ('(new Function(document.currentScript.previousSibling.textContent))();', True),
    ("setRead();", True),
    ("var alal = function() {", True),
    (";(function(){", True),
    ("他推开门走了进去。", False),
    ("“let me see，”她说。", False),
    ("第2章测试剧情", False),
])
def test_is_js_text(line, js):
    assert _is_js_text(line) == js


def test_extract_strips_js_text():
    """历史事故：wanwx 把 JS 放在裸文本节点，正文里混出 let urls=[...]。"""
    import lxml.html
    html = ('<div id="content"><div class="panel-body">'
            "<p>他推开门走了进去。</p><p>屋里很黑。</p></div>"
            "<div>let urls=[\"hUU\".split(\"UU\").join(\"\")];</div>"
            "<div>setRead();</div></div>")
    out = extract_content_text(lxml.html.fromstring(html))
    assert "let urls" not in out and "setRead" not in out
    assert "他推开门走了进去。" in out


# ---- garbled_ratio：字体反爬检测 ----
def test_garbled_ratio():
    """历史事故：铜雀台静态正文全是 CJK 扩展区映射字，detect 误选为正文容器。"""
    normal = "他推开门走了进去，屋里很黑。" * 30
    assert garbled_ratio(normal) == 0
    obf = "𤻁𢍓𢋎𧜉𦃟𣞱𧼦𥄼𢐗味𨐅。" * 30   # 字体映射乱码
    assert garbled_ratio(obf) > 0.5
    mixed = "正常句子若干。" * 10 + "𤻁𢍓𢋎" * 20
    assert 0.15 < garbled_ratio(mixed) < 0.9


# ---- bing_search L3：固定纯引号变体 ----
def test_bing_l3_pure_quote_only():
    """L3+作者词时必须只搜『"书名"』。历史事故：搜「书名 作者」L1 全降级后，
    L3 只试『"书名" 作者』变体，而该形态 Bing 即使真浏览器也返回百科/知乎
    垃圾，导致 0 结果（稳定命中小说站的只有纯引号形态）。"""
    from urllib.parse import quote
    from engine import websearch

    calls = []

    class FakeFetcher:
        def _req(self, method, url, **kw):
            calls.append(url)
            return None

    websearch.bing_search(FakeFetcher(), "测试长书名",
                          force_l3=True, author="测试作者")
    assert calls == ["https://cn.bing.com/search?q="
                     + quote('"测试长书名"')]


# ---- _relevance：搜索结果排序 ----
@pytest.mark.parametrize("kw,title,rel", [
    ("无敌测试皇", "无敌测试皇", 0),
    ("无敌测试皇", "无敌测试皇之再战", 1),
    ("测试长书名", "长书名", 2),
    ("无敌测试皇", "无关名著", 3),
])
def test_relevance(kw, title, rel):
    assert _relevance(kw, title) == rel


# ---- 域名混淆水印过滤 ----
def test_domain_watermark():
    text = "[全本小说]:qbxs. N e t 一秒记住！\n他推开门走了进去。\n另一段正文内容。"
    out = _filter_domain_watermark(text, "https://www.qbxs.net/read/1/2.html")
    assert "一秒记住" not in out and "他推开门" in out


# ---- 查询拆词：书名+作者 ----
def test_split_query():
    from engine.search import split_query
    assert split_query("测试长书名 某作者") == ("测试长书名", "某作者")
    assert split_query("测试长书名 by 某作者") == ("测试长书名", "某作者")
    assert split_query("测试长书名 作者：某作者")[1] == "某作者"
    assert split_query("测试长书名") == ("测试长书名", "")
    # 第二段像书名特征时不拆
    assert split_query("测试长书名 小说全集")[1] == ""


# ---- 缓存键 ----
def test_url_hash_stable():
    assert _url_hash("http://a/1.html") == _url_hash("http://a/1.html")
    assert _url_hash("http://a/1.html") != _url_hash("http://a/2.html")
