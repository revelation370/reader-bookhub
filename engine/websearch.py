# -*- coding: utf-8 -*-
"""搜索引擎发现层：Bing 搜「"书名" 小说/全文阅读」，提取小说站书籍页候选，
黑名单过滤 + 并行轻量预验（标题含书名），输出与种子站结果同构的列表。"""
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "autobs"))
from autobs import parse_doc, norm_space  # noqa: E402

# 非小说站黑名单（按主域名/关键词匹配）
DOMAIN_BL = (
    "jjwxc.net", "jjwxc.com",  # 用户不需要晋江（种子与发现层全部排除，含 wap/m 子域）
    "baidu.com", "bing.com", "google.com", "sogou.com", "so.com", "sm.cn",
    "zhihu.com", "weibo.com", "bilibili.com", "douban.com", "qq.com",
    "weixin.qq.com", "sohu.com", "163.com", "csdn.net", "jianshu.com",
    "toutiao.com", "douyin.com", "xiaohongshu.com", "taobao.com", "jd.com",
    "tmall.com",
    "zdic.net", "chagushici.com", "yuwenmi.com", "hanyuguoxue.com",
    "kmcha.com", "wanjuanguoxue.com", "moe.edu.tw", "guoxue",
    "wikipedia.org", "gov.cn", "edu.cn", "youtube.com", "facebook.com",
    "github.com", "cloud.tencent.com", "cnblogs.com", "cnstar",
    "music.163", "y.qq.com",
)

# 实测结论（2026-10）：Bing 上「纯引号短语」多数书命中最稳；
# 但部分书名（短词开头的长书名）引号查询会触发 Bing 的
# 单字字典模式，反而裸关键词能命中小说站——三种变体按序尝试、够数即停。
QUERY_VARIANTS = ('"{kw}"', '{kw}', '{kw} 小说')
MAX_VERIFY = 8          # 最多预验的候选数
VERIFY_TIMEOUT = 12


def _norm_title(s):
    return re.sub(r"[\s《》〈〉「」『』_:：\-—|·.。!！?？·,，()（）\[\]]+", "", s or "")


def _blacked(host):
    h = (host or "").lower()
    return any(b in h for b in DOMAIN_BL)


def _root_domain(host):
    parts = (host or "").lower().lstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def _bing_fetch(fetcher, query):
    """抓一页 Bing 结果，返回 doc 或 None。"""
    r = fetcher.get("https://cn.bing.com/search?q=" + query,
                    referer="https://cn.bing.com/")
    if r is None:
        return None
    return parse_doc(r)


def bing_search(fetcher, keyword, force_l3=False, author=""):
    """Bing 搜索一轮。force_l3=False 走普通 HTTP（快但易被降级），
    True 用真浏览器（慢 ~5s 但结果完整）。author 非空时启用联合查询
    变体（『"书名" 作者』——作者词能大幅过滤同名噪音，仅 L1 使用）。
    变体够数即停：L1 拿到 6 条可读链接后不再试后续变体（省 2.5s 间隔），
    L3 每次抓取 ~5s，拿到即停。返回 [(title, url)]。"""
    import time
    seen, results = set(), []

    def _collect(doc):
        for a in doc.xpath("//li[contains(@class,'b_algo')]//h2/a"):
            href = a.get("href") or ""
            title = norm_space(a.text_content())
            if not href.startswith("http") or not title:
                continue
            host = urlparse(href).netloc
            if not host or _blacked(host):
                continue
            if _norm_title(keyword) not in _norm_title(title):
                continue
            # 章节页直接丢弃（解析不出目录，用户点了必失败）：
            # 特征 = /read/书id/章节id 两层数字路径，或 _2.html 分页后缀。
            # 单层数字文件名（如 /yimo/3208398.html）是书页形态，保留。
            path = urlparse(href).path.lower()
            if (re.search(r"/reads?/\d+/\d+", path)
                    or re.search(r"/\d+/\d+(_\d+)?\.html?$", path)  # 任意 数字/数字.html 两层形态
                    or re.search(r"_\d+(\.html?)?$", path)):
                continue
            k = href.split("#")[0].rstrip("/")
            if k in seen:
                continue
            seen.add(k)
            results.append((title, href))

    variants = list(QUERY_VARIANTS)
    if author:
        variants = ['"{kw}" {au}'.format(kw=keyword, au=author)] + variants
    if force_l3:
        # L3 不用作者变体（『"书名" 作者』即使真浏览器 Bing 也返回百科/
        # 知乎类结果）；纯引号最稳，但部分书名会触发字典模式，
        # 用裸关键词兜底，拿到即停。
        variants = ['"{kw}"', '{kw}']
    enough = 1 if force_l3 else 6
    for i, tpl in enumerate(variants):
        if i:
            time.sleep(1.5 if force_l3 else 2.5)
        q = quote(tpl.format(kw=keyword, au=author))
        try:
            if force_l3:
                r = fetcher._req("GET", "https://cn.bing.com/search?q=" + q,
                                 referer="https://cn.bing.com/", force_layer="L3")
            else:
                r = fetcher.get("https://cn.bing.com/search?q=" + q,
                                referer="https://cn.bing.com/")
        except Exception:
            r = None
        if r is None:
            continue
        doc = parse_doc(r)
        if doc is not None:
            _collect(doc)
            if len(results) >= enough:
                break
    return results


def baidu_search(fetcher, keyword, max_links=10, author=""):
    """百度发现层（主引擎，2026-11-05 实测换位）：cn.bing 中文索引对小说站
    覆盖不稳且整时段轮换失明（同一批书名 Bing 全 0 条、百度 9/8/6 条，
    且百度连续 10 次脚本查询无验证页）。结果链接是 baidu.com/link 跳转链，
    需跟一次 Location 拿真实地址。返回 [(title, url)]。"""
    import time as _t
    _t.sleep(2)  # 与前置查询错开，降低连发被风控的概率
    q = quote(keyword + (" " + author if author else ""))
    r = fetcher.get("https://www.baidu.com/s?wd=" + q,
                    referer="https://www.baidu.com/")
    if r is None:
        return []
    doc = parse_doc(r)
    if doc is None:
        return []
    out, seen = [], set()
    for a in doc.xpath("//h3/a"):
        href = a.get("href") or ""
        title = norm_space(a.text_content())
        if not href or not title:
            continue
        if _norm_title(keyword) not in _norm_title(title):
            continue
        if href.startswith(("http://www.baidu.com/link", "https://www.baidu.com/link")):
            try:
                rr = fetcher.s.get(href, timeout=8, allow_redirects=False)
                href = rr.headers.get("Location") or ""
            except Exception:
                continue
        if not href.startswith("http"):
            continue
        host = urlparse(href).netloc
        if not host or _blacked(host):
            continue
        path = urlparse(href).path.lower()
        if (re.search(r"/reads?/\d+/\d+", path)
                or re.search(r"/\d+/\d+(_\d+)?\.html?$", path)
                or re.search(r"_\d+(\.html?)?$", path)):
            continue
        k = href.split("#")[0].rstrip("/")
        if k in seen:
            continue
        seen.add(k)
        out.append((title, href))
        if len(out) >= max_links:
            break
    return out


def _verify(fetcher, keyword, url):
    """打开候选页验证标题仍含书名。快探针模式（8s 单发、无重试升级）：
    打开书页要超过 8s 的站，进了结果也会在 Reader 打开时卡住，不值得收。
    通过时返回作者名（可为空串），否则 None。"""
    try:
        r = fetcher.get(url, quick=True)
    except Exception:
        return None
    if r is None:
        return None
    doc = parse_doc(r)
    if doc is None:
        return None
    text = _norm_title(" ".join(
        t for t in [e.text_content() for e in doc.xpath("//title | //h1")] if t))
    if _norm_title(keyword) not in text:
        return None
    try:
        from autobs import extract_book_info
        _, author = extract_book_info(doc, url)
        return author or ""
    except Exception:
        return ""


def _score_url(url):
    """书页特征加分、章节页降权（章节页解析不出目录）。"""
    s = 0
    path = urlparse(url).path.lower()
    if re.search(r"/book(\d*)/|/infos/|onebook\.php|/novel/\d+/?$", path):
        s += 2  # 书页/目录页特征
    if re.search(r"/reads?/\d+/\d+", path) or re.search(r"[_/]\d{5,}(_\d+)?\.html?$", path):
        s -= 1  # 章节页特征
    return s


def _verify_candidates(fetcher, keyword, cands, max_verify=MAX_VERIFY):
    """排序 + 并行预验，返回通过的书目 dict 列表。"""
    if not cands:
        return []
    cands.sort(key=lambda x: -_score_url(x[1]))
    by_root, ordered = {}, []
    for title, url in cands:
        rd = _root_domain(urlparse(url).netloc)
        if rd in by_root:
            continue
        by_root[rd] = url
        ordered.append((title, url))
    ordered = ordered[:max_verify]

    out = []
    # 不用 with 块：退出时会 join 全部线程，一个死站能把整轮验证拖到分钟级
    ex = ThreadPoolExecutor(max_workers=4)
    try:
        futs = {ex.submit(_verify, fetcher, keyword, url): (title, url)
                for title, url in ordered}
        for fut in as_completed(futs, timeout=VERIFY_TIMEOUT * 3):
            title, url = futs[fut]
            try:
                author = fut.result(timeout=VERIFY_TIMEOUT)
                if author is not None:
                    host = urlparse(url).netloc
                    out.append({"title": title[:60], "url": url,
                                "site": host.replace("www.", "").replace("m.", ""),
                                "src": "web", "author": author})
            except Exception:
                pass
    except Exception:
        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return out


def web_discover(fetcher, keyword, max_results=6, author=""):
    """发现层主入口（三阶段，百度主引擎 / Bing 备胎，2026-11-05 换位）：
    1) 百度（中文小说站覆盖最好）→ 预验
    2) 结果 <2 条 → Bing 补一轮（两家索引与风控轮换抽风，互为冗余）→ 预验
    3) 仍为空 → Bing 真浏览器再抓一轮 → 预验
    返回 [{'title','url','site','src':'web'}]。"""
    out = _verify_candidates(fetcher, keyword,
                             baidu_search(fetcher, keyword, author=author))
    if len(out) < 2:
        try:
            extra = _verify_candidates(fetcher, keyword,
                                       bing_search(fetcher, keyword, author=author))
        except Exception:
            extra = []
        if extra:
            have = {_root_domain(urlparse(r["url"]).netloc) for r in out}
            for r in extra:
                rd = _root_domain(urlparse(r["url"]).netloc)
                if rd not in have:
                    have.add(rd)
                    out.append(r)
    if not out:
        try:
            out = _verify_candidates(fetcher, keyword,
                                     bing_search(fetcher, keyword, force_l3=True,
                                                 author=author))
        except Exception:
            out = []
    return out[:max_results]
