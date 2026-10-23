# -*- coding: utf-8 -*-
"""聚合搜索：并发搜种子书站，返回统一结果列表。"""
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "autobs"))
from autobs import parse_doc, norm_space  # noqa: E402

SEEDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds.json")


def load_seeds():
    with open(SEEDS_PATH, encoding="utf-8") as fp:
        return json.load(fp)


def _search_site(fetcher, seed, keyword):
    """单站搜索，返回 [{'title','url','site'}] 或 []（失败）。"""
    charset = "gbk" if seed.get("charset") == "gbk" else "utf-8"
    kw = quote(keyword.encode(charset, errors="ignore"))
    url_tpl = seed["url"]
    if seed.get("method", "GET") == "POST":
        params = (seed.get("params") or "").replace("%s", kw)
        r = fetcher.post(url_tpl, data=params.encode("ascii"), referer=seed["host"])
    else:
        r = fetcher.get(url_tpl.replace("%s", kw), referer=seed["host"])
    if r is None:
        return []
    doc = parse_doc(r)
    if doc is None:
        return []
    try:
        names = doc.xpath(seed["name_xpath"])
        urls = doc.xpath(seed["mainpage_xpath"])
    except Exception:
        return []
    out = []
    from autobs import norm_space as _ns
    import re as _re
    rows = _pair_rows(names)
    for n, u in zip(names, urls):
        title = norm_space(n.text_content() if hasattr(n, "text_content") else str(n))
        if not title:
            continue
        out.append({
            "title": title[:60],
            "url": urljoin(r.url, u),
            "site": seed["name"],
            "author": _row_author(_row_of(n), title, _ns),
        })
        if len(out) >= 30:
            break
    return out


def _relevance(keyword, title):
    """精确匹配 > 包含关键词 > 关键词包含标题 > 其他。"""
    key = re.sub(r"\s+", "", keyword)
    t = re.sub(r"\s+", "", title)
    if not key:
        return 3
    if t == key:
        return 0
    if key in t:
        return 1
    if t in key:
        return 2
    return 3


def split_query(q):
    """『书名 作者』/『书名 by 作者』拆分：第二段短且非书名特征时视为作者。
    返回 (书名, 作者或'')。"""
    q = (q or "").strip()
    m = re.split(r"\s+by\s+|[\s　]+", q, maxsplit=1, flags=re.I)
    if len(m) == 2 and m[0] and m[1]:
        title, author = m[0].strip(), m[1].strip()
        author = re.sub(r"^作者[：:\s]*", "", author)
        if 1 < len(author) <= 12 and not re.search(r"小说|全集|番外|阅读", author):
            return title, author
    return q, ""


def aggregate_search(fetcher, keyword, seeds=None, timeout_per_site=40, web=True):
    """三层搜索：种子站直搜（并发）+ 搜索引擎发现层（Bing）。
    输入支持『书名 作者』联合查询：作者用于结果过滤排序，搜索用书名。
    返回扁平列表：作者匹配的最前，其余按相关度。"""
    keyword, want_author = split_query(keyword)
    seeds = seeds or load_seeds()
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=len(seeds) or 1) as ex:
        futs = {ex.submit(_search_site, fetcher, s, keyword): s for s in seeds}
        try:
            for fut in as_completed(futs, timeout=timeout_per_site * 2):
                seed = futs[fut]
                try:
                    results.extend(fut.result(timeout=5))
                except Exception as e:
                    errors.append({"site": seed["name"], "error": str(e)[:80]})
        except TimeoutError:
            # 慢站直接放弃，用已完成的结果（futures 会在 ex.shutdown 时被等掉）
            for fut, seed in futs.items():
                if fut.done() and not fut.cancelled():
                    try:
                        results.extend(fut.result(timeout=1))
                    except Exception:
                        pass
                elif not fut.done():
                    errors.append({"site": seed["name"], "error": "超时"})
    for r_ in results:
        r_["src"] = "seed"
    def _rank(x):
        rel = _relevance(keyword, x["title"])
        au = (x.get("author") or "")
        if want_author and (want_author in au or au in want_author and au):
            rel -= 1  # 作者匹配的书前置一级
        return rel

    results.sort(key=_rank)
    # 噪音治理：完全不含关键词的结果直接丢；"标题是关键词子串"的拆词噪音
    # （如搜「测试长书名」返回《交易》）每站最多 4 条；正常结果每站 12 条
    from collections import Counter
    per_site = Counter()
    capped = []
    for r_ in results:
        rel = _relevance(keyword, r_["title"])
        if rel >= 3:
            continue
        site = r_.get("site", "")
        limit = 4 if rel == 2 else 12
        if per_site[site] >= limit:
            continue
        per_site[site] += 1
        capped.append(r_)
    results = capped

    # 发现层：Bing 全网找新站（种子站没有的书靠这层覆盖）
    web_results = []
    if web:
        try:
            try:
                from engine.websearch import web_discover
            except ImportError:
                from websearch import web_discover
            web_results = web_discover(fetcher, keyword)
        except Exception as e:
            errors.append({"site": "web", "error": str(e)[:80]})

    # 联合查询（带作者）时，发现层结果与种子结果统一按作者匹配度重排，
    # 否则目标书（常来自发现层）会整体沉底
    if want_author and web_results:
        merged0 = results + web_results
        merged0.sort(key=_rank)
        results, web_results = merged0[:len(results)], merged0[len(results):]

    # 全局重排：种子站与发现层结果同台按相关度排序——
    # 发现层的精确匹配结果不应被种子站的拆词噪音（如搜长书名返回的单字书）压制
    if web_results:
        results = results + web_results
        results.sort(key=_rank)
        web_results = []

    # 同 URL / 同主域去重：种子站结果优先于网页发现
    seen_url, seen_root, uniq = set(), set(), []
    for r_ in results + web_results:
        from urllib.parse import urlparse as _up
        k = r_["url"].rstrip("/")
        if k in seen_url:
            continue
        root = ".".join((_up(r_["url"]).netloc.lower().lstrip(".").split(".")[-2:]))
        # 种子结果允许保留（它们本来就不同站）；web 结果若与已有主域重复则丢弃
        if r_.get("src") == "web" and root in seen_root:
            continue
        seen_url.add(k)
        seen_root.add(root)
        uniq.append(r_)
    return uniq[:60], errors


def maybe_add_seed(src, fetcher):
    """解析成功后自动沉淀种子站：该站站内搜索可用（query_url 含 %s）且
    真实搜一次能出结果，才写入 seeds.json。返回新增的 seed 或 None。"""
    try:
        if not src or "%s" not in (src.get("query_url") or ""):
            return None  # 单书模式/无搜索：不沉淀
        host = src.get("host") or ""
        seeds = load_seeds()
        if any(_root(s.get("host", "")) == _root(host) for s in seeds):
            return None  # 已有该站
        seed = {
            "name": (src.get("title") or host)[:20],
            "host": host,
            "method": "POST" if src.get("query_method") == 1 else "GET",
            "url": src["query_url"],
            "params": src.get("query_params") or "",
            "charset": "gbk" if src.get("query_charset") == 2 else "utf-8",
            "name_xpath": src.get("book_name_xpath") or "",
            "mainpage_xpath": src.get("book_mainpage_xpath") or "",
        }
        # 搜索端点真实性验证：搜本书名（前 2 字泛化）能出结果才收
        got = _search_site(fetcher, seed, seed["name"][:2] or "小说")
        if not got:
            return None
        seeds.append(seed)
        tmp = SEEDS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(seeds, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, SEEDS_PATH)  # 原子替换，避免写入中断损坏种子库
        return seed
    except Exception:
        return None


def _root(host):
    parts = (host or "").lower().lstrip(".").split(".")
    return ".".join(parts[-2:])


def _row_of(el, max_up=3):
    """书名元素向上找行容器（文本明显多于书名本身的最小祖先）。"""
    row = el
    for _ in range(max_up):
        p = row.getparent()
        if p is None:
            break
        row = p
        if len(norm_space(row.text_content() or "")) > 20:
            return row
    return row


def _pair_rows(names):
    return None  # 兼容占位（行容器在 _row_author 内部按需获取）


def _row_author(row, title, ns):
    """从搜索结果行提取作者：两种常见形态——
    1) 杰奇系行文本『书名 连载中作者：XX 最新章节』（正则）
    2) 列布局行内无链接短文本元素（晋江式 span）"""
    try:
        text = ns(row.text_content() or "")
        m = re.search(r"作者[：:\s]*([^\s　,，第（(]{1,20})", text)
        if m:
            return m.group(1)[:20]
        for el in row.iter("span", "td", "em", "p"):
            if list(el.iter("a")):
                continue
            txt = ns(el.text_content() or "")
            if 2 <= len(txt) <= 12 and txt != title                     and not re.search(r"\d|连载|完本|小说|阅读|章", txt):
                return txt[:20]
    except Exception:
        pass
    return ""
