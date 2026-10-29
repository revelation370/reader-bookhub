# -*- coding: utf-8 -*-
"""
autobs - Reader(binbyu/Reader) 书源自动配置工具

输入一个书籍目录页 URL，自动分析并生成可导入 Reader 的书源 bs.json：
  1. 章节列表 xpath（标题/URL），检测章节列表分页
  2. 章节正文 xpath（单容器），检测正文分页
  3. 搜索配置：自动发现站内搜索表单，用书名真实搜索并"按已知答案反推"xpath
  4. 生成后先用规则真实验证一遍，通过才写出 bs.json

用法:
  python autobs.py <书籍目录页URL> [--title 书名] [--author 作者] [--name 书源名]
                                   [-o 输出文件.json] [--no-search] [--debug]

依赖: requests, lxml
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from urllib.parse import quote, urljoin, urlparse, urlunparse

import requests
import lxml.html

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Reader.exe 内置的 UA（从二进制提取），用于"Reader 仿真预检"
READER_UA = ("Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/67.0.3396.99 Safari/537.36")

# Cloudflare 等 WAF 挑战页特征
WAF_MARKERS = ("just a moment", "cf-challenge", "cf_chl", "checking your browser",
               "访问验证", "安全验证", "正在验证")

NEXT_TEXTS = ("下一页", "下页")
HOP_TEXTS = ("开始阅读", "查看目录", "目录", "最新章节列表", "全部章节")
CONTENT_HINT = re.compile(
    r"(content|booktext|readarea|chaptercontent|htmlcontent|showtxt|"
    r"noveltext|readbox|article|text|txt|read)", re.I)
JUNK_HEAD = re.compile(
    r"^(双击|当前位置|您的位置|位置[:：]|首页[\s>»/]|快捷键"
    r"|加入书架|阅读设置|返回目录|章节目录|上一章|下一章|上一页"
    r"|【收藏此书】|手机阅读|报错|目录$)")
NAV_TEXT = re.compile(
    r"^(首页|上一页|下一页|上一章|下一章|目录|书签|收藏|推荐|排行|登录|注册|"
    r"加入书架|投.*票|手机阅读|返回|评论|报错|下载|完本|连载).*$")

# 站方 SEO 推广尾（出现在链接文本末尾 = 推广位而非章节列表）
SEO_TAIL = re.compile(
    r"(免费阅读|笔趣阁|最新章节|无弹窗|全文阅读|无广告|手机阅读|txt下载)\s*$")

# 章节式标题开头（章节列表 vs 推荐位的强区分信号）
CHAPTERISH_HEAD = re.compile(
    r"^(第\s*[\d一二三四五六七八九十百千零〇两]+[章节回卷部]|番外|楔子|序章|序幕|尾声|"
    r"Chapter\s*\d+|\d{1,4}[、.．]\s*\S)")

COMMON_SEARCH = [
    ("GET", "/search.php?searchkey=%s", None),
    ("GET", "/s.php?q=%s", None),
    ("GET", "/search?keyword=%s", None),
    ("GET", "/search.php?kw=%s&t=1", None),   # 晋江式
    ("POST", "/modules/article/search.php", "searchkey=%s&searchtype=articlename"),
    ("POST", "/e/search/index.php", "keyboard=%s&show=title"),
]

CSRF_NAME = re.compile(r"(?i)^_?token$|csrf|authenticity_token")


def log(level, msg):
    print("[%s] %s" % (level, msg))


def norm_space(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


# 反爬混淆常见形态：JS 源码放在裸文本节点（非 <script> 内），由相邻
# <script> 取 previousSibling.textContent 执行（wanwx 系：let urls=[...]
# .split("UU").join("")...）。clean_text/extract 都要剔除这类文本。
_JS_OBFUSC = re.compile(r'\.split\("[^"]{1,4}"\)\.join\(')
_JS_LINE_START = re.compile(
    r"(let |var |const |function |window\.|document\.|navigator\.|;"
    r"\(function|\(function|\(new |new |if\(|if \(|return |eval\("
    r"|try\{|try \{|typeof )")
_JS_CALL_LINE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.]*\([^()]*\);?$")


def _is_js_text(s):
    """整块文本（文本节点/整行）是否为 JS 源码。中文正文不会误伤。"""
    s = (s or "").strip()
    if len(s) < 6:
        return False
    if _JS_OBFUSC.search(s):
        return True
    return bool(_JS_LINE_START.match(s) or _JS_CALL_LINE.match(s))


def garbled_ratio(text):
    """字体反爬检测：文本中 CJK 扩展区/私用区字符占比。
    自定义字体映射站（如铜雀台）静态 HTML 的正文全是生僻字映射，
    正常中文正文该占比几乎为 0。"""
    n = max(1, len(text))
    ext = sum(1 for c in text
              if "\U00020000" <= c <= "\U0003ffff" or "\ue000" <= c <= "\uf8ff")
    return ext / n


def clean_text(el):
    """元素文本（排除 script/style 子树与 JS 样裸文本节点），用于正文度量。"""
    try:
        parts = el.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]")
        return norm_space(" ".join(t for t in parts if t and not _is_js_text(t)))
    except Exception:
        return norm_space(el.text_content())


def norm_url(u):
    """归一化 URL 用于比较：去末尾斜杠/index.xxx/锚点，host 小写。"""
    try:
        p = urlparse(u)
        path = re.sub(r"/(index\.(html?|shtml|php|asp|jsp))?$", "/", p.path)
        path = re.sub(r"/+$", "/", path)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))
    except Exception:
        return u


class Fetcher:
    def __init__(self, debug=False):
        self.s = requests.Session()
        self.s.trust_env = False  # 书站直连，绕过系统代理（代理常致 RST/超时）
        self.s.headers.update({"User-Agent": UA,
                               "Accept-Language": "zh-CN,zh;q=0.9"})
        self.last = 0.0
        self.debug = debug

    def get(self, url, **kw):
        return self._req("GET", url, **kw)

    def post(self, url, data=None, **kw):
        return self._req("POST", url, data=data, **kw)

    def _req(self, method, url, data=None, referer=None, _retried=False):
        # 限速，友好请求
        dt = time.time() - self.last
        if dt < 0.6:
            time.sleep(0.6 - dt)
        self.last = time.time()
        headers = {}
        if referer:
            headers["Referer"] = referer
        if isinstance(data, (bytes, str)):
            # 预编码的表单体必须显式声明，否则 PHP 不解析 $_POST
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(3):
            try:
                if method == "GET":
                    r = self.s.get(url, headers=headers, timeout=15)
                else:
                    r = self.s.post(url, data=data, headers=headers, timeout=15)
                if self.debug:
                    log("DEBUG", "%s %s -> %s (%d bytes)" % (method, url, r.status_code, len(r.content)))
                if r.status_code != 200:
                    return None
                return r
            except requests.SSLError:
                if not _retried:
                    self.s.verify = False
                    import urllib3
                    urllib3.disable_warnings()
                    log("WARN", "证书校验失败，已关闭 SSL 校验重试")
                    return self._req(method, url, data=data, referer=referer, _retried=True)
                return None
            except requests.RequestException as e:
                if attempt < 2:
                    time.sleep(2 + attempt * 3)  # 站点限速常见，退避重试
                    continue
                log("WARN", "请求失败 %s: %s" % (url, e))
                return None


def parse_doc(resp):
    """从响应构造 lxml 文档。
    - 普通响应：按 bytes + meta 声明处理编码
    - 浏览器层响应（browser=True）：text 已是解码好的字符串，直接用，
      避免 lxml 对无 meta 声明的 bytes 猜错编码（中文变 mojibake）
    拆掉 tbody：lxml 会自动补 tbody 而 Reader(libxml2 原生)不会，
    保持一致才能保证生成的 xpath 在 Reader 里可用。"""
    doc = None
    if getattr(resp, "browser", False) and resp.text:
        try:
            doc = lxml.html.fromstring(resp.text, base_url=resp.url)
        except Exception:
            doc = None
    if doc is None:
        try:
            doc = lxml.html.fromstring(resp.content, base_url=resp.url)
        except Exception:
            try:
                doc = lxml.html.fromstring(resp.text)
            except Exception:
                return None
    for tb in doc.xpath("//tbody"):
        tb.drop_tag()
    return doc


def doc_text(doc):
    try:
        return doc.text_content()
    except Exception:
        return ""


def abs_path(el):
    """/html/body/div[2]/ul 形式（tag[在同名兄弟中的序号]），跳过 tbody。"""
    steps = []
    cur = el
    while cur is not None and cur.tag not in (None,) and isinstance(cur.tag, str):
        if cur.tag.lower() == "tbody":  # Reader(libxml2) 不会自动补 tbody
            cur = cur.getparent()
            continue
        parent = cur.getparent()
        if parent is None or cur.tag.lower() == "html":
            steps.append(cur.tag.lower())
            break
        same = [s for s in parent if isinstance(s.tag, str) and s.tag.lower() == cur.tag.lower()]
        pos = same.index(cur) + 1 if len(same) > 1 else None
        steps.append("%s[%d]" % (cur.tag.lower(), pos) if pos else cur.tag.lower())
        cur = parent
    return "/" + "/".join(reversed(steps))


def unique_xpath(el, doc):
    """优先 id，其次 class，最后绝对路径。返回使匹配数尽量唯一且包含 el 的 xpath。"""
    eid = el.get("id")
    if eid and re.fullmatch(r"[A-Za-z][\w\-.:]*", eid) and '"' not in eid:
        xp = "//%s[@id=\"%s\"]" % (el.tag.lower(), eid)
        try:
            if len(doc.xpath(xp)) >= 1:
                return xp
        except Exception:
            pass
    cls = norm_space(el.get("class"))
    if cls and len(cls) <= 60 and '"' not in cls:
        xp = "//%s[@class=\"%s\"]" % (el.tag.lower(), cls)
        try:
            if len(doc.xpath(xp)) >= 1:
                return xp
        except Exception:
            pass
    return abs_path(el)


def common_ancestor(els):
    paths = [list(reversed(list(e.iterancestors()))) for e in els]
    if not paths:
        return None
    anc = None
    for nodes in zip(*paths):
        if all(n is nodes[0] for n in nodes):
            anc = nodes[0]
        else:
            break
    return anc


def rel_tag_path(container, el):
    """container 到 el 的相对 tag 路径，如 dd/a；el==container 时返回 ''。"""
    tags = []
    cur = el
    while cur is not None and cur is not container:
        tags.append(cur.tag.lower())
        cur = cur.getparent()
    if cur is None:
        return None
    return "/".join(reversed(tags))


def _chap_num(t):
    """标题里的章节号（第X章 / X. 两种形态），取不到返回 None。"""
    m = re.match(r"^\s*(?:第\s*(\d+)\s*章|(\d+)\s*[.、])", t or "")
    return int(m.group(1) or m.group(2)) if m else None


def group_anchors(doc, min_count=5):
    """把页面里的 <a> 按父级结构签名分组，返回 [(分数, anchors)] 降序。"""
    anchors = []
    for a in doc.iter("a"):
        href = a.get("href") or ""
        text = norm_space(a.text_content())
        if not href or href.startswith(("#", "javascript")):
            continue
        if not (1 <= len(text) <= 60):
            continue
        anchors.append(a)

    groups = defaultdict(list)
    for a in anchors:
        sig = []
        cur = a.getparent()
        for _ in range(4):  # 向上4层签名，区分"最新章节"与"全部章节"等不同区块
            if cur is None or not isinstance(cur.tag, str):
                sig.append(("", ""))
            else:
                sig.append((cur.tag.lower(), norm_space(cur.get("class"))[:40]))
                cur = cur.getparent()
        groups[tuple(sig)].append(a)

    scored = []
    for sig, items in groups.items():
        if len(items) < min_count:
            continue
        hrefs = [a.get("href") for a in items]
        texts = [norm_space(a.text_content()) for a in items]
        nav = sum(1 for t in texts if NAV_TEXT.match(t))
        if nav > len(items) * 0.3:
            continue
        # 推广链接组剔除：文本以站方 SEO 尾巴结尾（"XX了免费阅读笔趣阁"式），
        # 真章节标题不会以这些词收尾
        seo = sum(1 for t in texts if SEO_TAIL.search(t))
        if seo > len(items) * 0.3:
            continue
        dup_ratio = 1 - len(set(hrefs)) / len(hrefs)
        # href 同质性：公共目录前缀 或 数字结尾比例
        prefix = hrefs[0]
        for h in hrefs[1:]:
            while not h.startswith(prefix) and prefix:
                prefix = prefix[:-1]
        num_ratio = sum(1 for h in hrefs if re.search(r"\d+", h)) / len(hrefs)
        score = len(items) * (1 - dup_ratio) * (0.6 + 0.4 * num_ratio)
        # 章节式标题占比：真章节列表的标题天然以"第X章/番外/楔子"等开头
        # （推广/推荐组的标题是书名或问答句）——强区分信号，直接乘权
        chapterish = sum(1 for t in texts if CHAPTERISH_HEAD.match(t))
        score *= (0.3 + 1.7 * chapterish / len(items))
        # 「最新章节」侧栏识别：组内章节号整体递减（如 279.→172. 倒序块），
        # 完整目录几乎总是正序或低章号开头。倒序块降权——数量再多也争不过
        # 正序主目录；仅当页面没有正序候选时才可能胜出。
        nums = [_chap_num(t) for t in texts]
        pairs = [(a, b) for a, b in zip(nums, nums[1:]) if a and b]
        if pairs and sum(1 for a, b in pairs if a > b) / len(pairs) >= 0.8:
            score *= 0.4
        if len(prefix) > len("/"):
            score *= 1.2
        scored.append((score, items))
    scored.sort(key=lambda x: -x[0])
    return scored


def build_list_xpath(anchors, doc):
    """由一组章节链接生成 (标题xpath, URLxpath)。失败返回 None。"""
    want = {id(a) for a in anchors}

    def exact(xp):
        try:
            got = doc.xpath(xp)
        except Exception:
            return False
        return bool(got) and {id(e) for e in got} == want

    # 策略1：父元素签名。父级 (tag,class) 一致时直接 //父tag[@class='x']/a，
    # 对"dd 未闭合导致层层嵌套"等畸形 HTML 也稳健。
    # 允许 xpath 多匹配少量同组外链接（同一列表被分组签名拆散的姊妹块）。
    parents = [a.getparent() for a in anchors]
    ptags = {p.tag.lower() for p in parents if p is not None and isinstance(p.tag, str)}
    if len(ptags) == 1 and len(parents) == len(anchors):
        ptag = ptags.pop()

        def superset_ok(xp):
            try:
                got = doc.xpath(xp)
            except Exception:
                return False
            gotids = {id(e) for e in got}
            return want <= gotids and len(got) >= 3 and len(got) <= max(len(want) * 1.5, len(want) + 10)

        pclasses = {norm_space(p.get("class")) for p in parents}
        pids = {p.get("id") for p in parents}
        if len(pids) == 1 and pids.pop():
            pid = parents[0].get("id")
            if '"' not in pid:
                xp = "//%s[@id=\"%s\"]/a" % (ptag, pid)
                if superset_ok(xp):
                    return xp, xp + "/@href"
        if len(pclasses) == 1:
            pcls = pclasses.pop()
            if pcls and len(pcls) <= 60 and '"' not in pcls:
                xp = "//%s[@class=\"%s\"]/a" % (ptag, pcls)
                if superset_ok(xp):
                    return xp, xp + "/@href"

    # 策略2：公共祖先 + 统一相对路径
    container = common_ancestor(anchors)
    if container is None:
        return None
    rels = {rel_tag_path(container, a) for a in anchors}
    if len(rels) != 1:
        return None
    rel = rels.pop()
    base_pair = None
    for base in (unique_xpath(container, doc), abs_path(container)):
        xp = base + ("/" + rel if rel else "")
        if exact(xp):
            return xp, xp + "/@href"
        base_pair = base_pair or (base, xp)

    # 策略3：相对路径过宽时，在路径的某一层加"组内共有属性谓词"收敛
    # （如 jjwxc 章节行的 tr[@itemprop='chapter']）
    if base_pair:
        base, wide_xp = base_pair
        try:
            wide_got = doc.xpath(wide_xp)
        except Exception:
            wide_got = []
        if wide_got and want < {id(e) for e in wide_got}:
            steps = rel.split("/") if rel else []
            # 每个锚点沿 rel 路径自底向上各层的元素（锚点本身是最后一层，深度0）
            for lvl in range(len(steps) - 1, -1, -1):
                depth = len(steps) - 1 - lvl  # 该层距锚点的层数
                lvl_els = []
                for a in anchors:
                    cur = a
                    for _ in range(depth):
                        cur = cur.getparent()
                        if cur is None:
                            break
                    if cur is None:
                        break
                    lvl_els.append(cur)
                if len(lvl_els) != len(anchors):
                    continue
                # 组内共有属性（排除 style/align 等无区分度的）
                common = dict(lvl_els[0].attrib)
                for e in lvl_els[1:]:
                    for k in list(common):
                        if e.get(k) != common[k]:
                            common.pop(k)
                for k, v in common.items():
                    if k in ("style", "align", "width", "height") or not v or '"' in v:
                        continue
                    steps2 = list(steps)
                    steps2[lvl] = '%s[@%s="%s"]' % (steps2[lvl], k, v)
                    xp2 = base + "/" + "/".join(steps2)
                    if exact(xp2):
                        return _maybe_better_title(xp2, base, steps2, lvl, anchors, doc)
            # 策略4：按 href 公共 token 收敛（章节链接常含公共片段，如 chapterid=）
            hrefs = [a.get("href") or "" for a in anchors]
            for token in common_href_tokens(hrefs):
                steps4 = list(steps)
                steps4[-1] = "a[contains(@href,'%s')]" % token
                xp4 = base + "/" + "/".join(steps4)
                if exact(xp4):
                    return _maybe_better_title(xp4, base, steps4, 0, anchors, doc,
                                               href_token=token)
    return None


def common_href_tokens(hrefs):
    """所有 href 的最长公共子串候选（优先不含数字的通用片段，避免绑定单一书籍）。
    返回按优先级排序的候选列表。"""
    import difflib
    hrefs = [h for h in hrefs if h]
    if not hrefs:
        return []
    cur = hrefs[0]
    for h in hrefs[1:]:
        m = difflib.SequenceMatcher(None, cur, h).find_longest_match(0, len(cur), 0, len(h))
        cur = cur[m.a:m.a + m.size]
        if not cur:
            return []
    cands = []
    # 完整公共子串（可能含书籍id数字，优先级最低）
    if len(cur) >= 4:
        cands.append(cur)
    # 按数字切分后的无数字片段（更通用，优先）
    for piece in re.split(r"\d+", cur):
        piece = piece.strip("&?/=._-")
        if len(piece) >= 4 and re.search(r"[a-zA-Z一-鿿]", piece):
            cands.append(piece)
    # 去重并排序：无数字的长片段优先
    seen, out = set(), []
    for c in sorted(cands, key=lambda x: (bool(re.search(r"\d", x)), -len(x))):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


NUMLIKE_TITLE = re.compile(r"^[第章回卷部]?[\d一二三四五六七八九十百千零〇两]+[章节回卷集]?$")
DATE_LIKE = re.compile(r"\d{4}[-/年]\d{1,2}|更新时间|首发时间")


def _maybe_better_title(url_xp, base, steps, row_lvl, anchors, doc, href_token=None):
    """URL 规则已精确。若锚点文本只是序号（'章一'），尝试用行内最长文本的
    同列单元格做标题（晋江式表格结构）。"""
    n_num = sum(1 for a in anchors if NUMLIKE_TITLE.match(norm_space(a.text_content())))
    if n_num < len(anchors) * 0.8:
        return url_xp, url_xp + "/@href"
    if href_token:
        # href 收敛过的行集合需要用嵌套谓词表达，保证标题数与URL数一致
        row_xp = base + "/" + steps[row_lvl] + "[.//" + steps[-1] + "]"
    else:
        row_xp = base + "/" + "/".join(steps[:row_lvl + 1])
    try:
        rows = doc.xpath(row_xp)
    except Exception:
        return url_xp, url_xp + "/@href"
    if len(rows) != len(anchors):
        return url_xp, url_xp + "/@href"
    # 在每行内找"文本最长且不含链接"的同列单元格
    def cells_of(row):
        return [c for c in row if isinstance(c.tag, str) and c.tag.lower() in ("td", "th", "div", "span")]
    best_idx, best_score = None, 0
    ncol = len(cells_of(rows[0]))
    for i in range(ncol):
        lens, linked = [], False
        for row in rows[:10]:
            cells = cells_of(row)
            if i >= len(cells):
                lens.append(0)
                continue
            c = cells[i]
            if list(c.iter("a")):
                linked = True
            txt = norm_space(c.text_content())
            if DATE_LIKE.search(txt):  # 跳过时间列
                linked = True
            lens.append(len(txt))
        avg = sum(lens) / max(1, len(lens))
        if not linked and avg > best_score:
            best_idx, best_score = i, avg
    if best_idx is None or best_score < 2:
        return url_xp, url_xp + "/@href"
    title_xp = "%s/*[%d]" % (row_xp, best_idx + 1)
    try:
        got = doc.xpath(title_xp)
        texts = [norm_space(e.text_content()) for e in got]
    except Exception:
        return url_xp, url_xp + "/@href"
    if len(got) != len(anchors) or sum(1 for t in texts if not t) > len(texts) * 0.5:
        return url_xp, url_xp + "/@href"
    return title_xp, url_xp + "/@href"


def find_next_page_anchor(doc, page_url):
    """在页面上找"下一页"链接，返回 (元素, 绝对URL) 或 None。"""
    for a in doc.iter("a"):
        text = norm_space(a.text_content())
        if text in NEXT_TEXTS:
            href = a.get("href") or ""
            if href and not href.startswith(("#", "javascript")):
                return a, urljoin(page_url, href)
    return None, None


def extract_book_info(doc, page_url):
    title, author = "", ""
    og_novel, og_title = "", ""
    for m in doc.iter("meta"):
        prop = (m.get("property") or m.get("name") or "").lower()
        if prop in ("og:novel:book_name", "og:novel:bookname", "novel:book_name"):
            og_novel = og_novel or norm_space(m.get("content"))
        elif prop == "og:title":
            og_title = og_title or norm_space(m.get("content"))
        elif prop in ("og:novel:author", "novel:author"):
            author = author or norm_space(m.get("content"))
    h1_text = ""
    h1 = doc.xpath("//h1")
    if h1:
        h1_text = norm_space(h1[0].text_content())
    # og:novel:book_name 最干净；h1 通常是纯书名；og:title 常带 SEO 后缀，排最后
    title = og_novel or h1_text or og_title
    if not title:
        t = doc.xpath("//title")
        if t:
            raw = norm_space(t[0].text_content())
            raw = re.split(r"[_\-—|]|最新章节|全文阅读|无弹窗", raw)[0]
            title = raw.strip()
    if not author:
        m = re.search(r"作\s*者\s*[:：]\s*([^\s，,。/|]{1,20})", doc_text(doc))
        if m:
            author = re.split(r"更新|点击|字数|状态|连载|完本", m.group(1))[0].strip("：: ")
    # 标题清理：去掉站名后缀 "书名_作者_站名" 之类
    title = re.split(r"[_\-—|]", title)[0].strip() if "_" in title else title
    return title, author


def _descend_chain(el):
    """从候选容器向下收缩到最干净的容器链，返回 [el, child, ...]。
    下降条件：某个元素孩子文本占比 ≥60%，且第二大的元素孩子 <25%（排除正文
    被拆成多块的情况，避免下降后丢内容）。"""
    chain = [el]
    cur = el
    while True:
        cur_len = len(norm_space(cur.text_content()))
        if cur_len <= 300:
            break
        sizes = []
        for c in cur:
            if isinstance(c.tag, str) and c.tag.lower() not in ("script", "style"):
                sizes.append((len(norm_space(c.text_content())), c))
        if not sizes:
            break
        sizes.sort(key=lambda x: -x[0])
        top_n, top_c = sizes[0]
        second_n = sizes[1][0] if len(sizes) > 1 else 0
        if top_n >= cur_len * 0.6 and top_n > 300 and second_n < cur_len * 0.25:
            chain.append(top_c)
            cur = top_c
            if len(chain) > 8:
                break
        else:
            break
    return chain


def detect_content_xpath(chapter_docs):
    """在若干章节页面上找正文容器 xpath。
    评分基于"干净文本"（剔除 script/style），再按 id/class 线索、链接密度、
    头部杂质调整；候选生成 xpath 后在所有采样页验证恰好1个匹配。"""
    per_page = []
    for doc in chapter_docs:
        cands = []
        for el in doc.iter("div", "article", "section", "td"):
            ident = "%s %s" % (el.get("id") or "", el.get("class") or "")
            text = clean_text(el)
            n = len(text)
            if n < 300:
                continue
            if garbled_ratio(text) > 0.15:
                continue  # 字体反爬：静态文本是生僻字映射，抓了也是乱码
            link_text = sum(len(clean_text(a)) for a in el.iter("a"))
            density = link_text / max(1, n)
            if density > 0.25:
                continue
            score = n * (1.6 if CONTENT_HINT.search(ident) else 1.0) * (1 - density)
            if JUNK_HEAD.search(text[:60]):  # 容器混入面包屑/快捷提示等杂质，降权
                score *= 0.55
            # p 标签文本占比：正文章节几乎都是 <p>段落</p> 结构（0.9+），
            # 而把导航/推荐区一并包住的大容器该值明显更低——用它区分"刚好"和"包多了"
            p_text = sum(len(clean_text(p)) for p in el.iter("p"))
            score *= (0.3 + 0.7 * (p_text / n)) if n else 1.0
            cands.append((score, el))
        if not cands:
            return None
        cands.sort(key=lambda x: -x[0])
        # 后代若已包含祖先 ≥90% 的干净文本，祖先只是多了层外壳（面包屑/标题/翻页），去掉祖先
        top_pool = cands[:15]
        drop = set()
        texts = {id(e): len(clean_text(e)) for _, e in top_pool}
        for s1, e1 in top_pool:
            for s2, e2 in top_pool:
                if e1 is e2 or id(e1) in drop:
                    continue
                anc = e2
                while anc is not None:
                    if anc is e1:
                        if texts[id(e2)] >= texts[id(e1)] * 0.9:
                            drop.add(id(e1))
                        break
                    anc = anc.getparent()
        cands = [(s, e) for s, e in cands if id(e) not in drop]
        per_page.append([el for _, el in cands[:3]])

    def valid_on_all(xp):
        for doc in chapter_docs:
            try:
                got = doc.xpath(xp)
            except Exception:
                return False
            if len(got) != 1 or len(clean_text(got[0])) < 200:
                return False
        return True

    for top in per_page[0]:
        # 候选自身优先（评分已含杂质惩罚，干净容器分数最高）
        xp = unique_xpath(top, chapter_docs[0])
        if valid_on_all(xp):
            return xp
        # 失败再沿向下收缩链回退（跳过已试过的 top 自身）
        for cand in list(reversed(_descend_chain(top)))[:-1]:
            xp = unique_xpath(cand, chapter_docs[0])
            if valid_on_all(xp):
                return xp
    return None


def verify_content_pagination(fetcher, first_url, content_xp, next_el):
    """验证正文分页：跟随"下一页"直到关键字消失或超页数。返回 (ok, pages_text_len)。"""
    url = urljoin(first_url, next_el.get("href"))
    seen = {norm_url(first_url)}
    total = 0
    for _ in range(6):
        if norm_url(url) in seen:
            return False, 0
        seen.add(norm_url(url))
        r = fetcher.get(url, referer=first_url)
        if r is None:
            return False, 0
        doc = parse_doc(r)
        if doc is None:
            return False, 0
        got = doc.xpath(content_xp)
        if len(got) != 1:
            return False, 0
        total += len(norm_space(got[0].text_content()))
        nxt, _ = find_next_page_anchor(doc, url)
        if nxt is None:
            return True, total  # 正常终止
        url = urljoin(url, nxt.get("href"))
    return False, 0  # 超过6页，判定为不可靠


def detect_chapter_pagination(fetcher, list_url, title_xp, first_hrefs):
    """检测章节列表分页。返回 enable 时需要的 (next_url_xp, next_kw_xp) 或 None。"""
    doc_first = parse_doc(fetcher.get(list_url))
    if doc_first is None:
        return None
    nxt, next_url = find_next_page_anchor(doc_first, list_url)
    if nxt is None:
        return None
    r = fetcher.get(next_url, referer=list_url)
    if r is None:
        return None
    doc2 = parse_doc(r)
    if doc2 is None:
        return None
    try:
        hrefs2 = doc2.xpath(title_xp + "/@href")
    except Exception:
        return None
    if not hrefs2 or set(hrefs2) <= set(first_hrefs):
        return None  # 第二页没有新章节
    # 生成"下一页"按钮 xpath（用第一页的元素）
    xp = unique_xpath(nxt, doc_first)
    try:
        if not doc_first.xpath(xp):
            return None
    except Exception:
        return None
    return xp + "/@href", xp


class SearchDetector:
    def __init__(self, fetcher, root_url, book_url, title):
        self.f = fetcher
        self.root = root_url
        self.book_url = book_url
        self.title = title
        # 书名变体：原名 + 去掉"749："式序号前缀的版本
        variants = [title]
        stripped = re.sub(r"^\d{1,5}\s*[：:、.．\-]\s*", "", title or "")
        if stripped and stripped != title:
            variants.append(stripped)
        self.titles = [t for t in variants if t]

    # ---------- 表单发现 ----------
    def find_forms(self):
        r = self.f.get(self.root)
        if r is None:
            return []
        doc = parse_doc(r)
        if doc is None:
            return []
        forms = []
        for form in doc.iter("form"):
            action = form.get("action") or self.root
            action = urljoin(self.root, action)
            method = (form.get("method") or "get").lower()
            inputs = []
            for inp in form.iter("input"):
                name = inp.get("name")
                if not name:
                    continue
                typ = (inp.get("type") or "text").lower()
                if typ in ("submit", "button", "image", "reset", "file"):
                    continue
                if CSRF_NAME.search(name):
                    continue  # CSRF token 随会话轮换，静态书源无法使用，直接跳过
                inputs.append((name, inp.get("value") or "", typ))
            kw_idx, score = -1, 0
            for i, (name, val, typ) in enumerate(inputs):
                ln = name.lower()
                if ln in ("searchkey", "q", "wd", "keyword", "search", "s",
                          "bookname", "key", "keyboard", "search_key", "query"):
                    kw_idx, score = i, score + 3
                elif "search" in ln or "key" in ln:
                    if kw_idx < 0:
                        kw_idx = i
                    score += 1
            if kw_idx < 0:
                text_inputs = [i for i, (_, _, t) in enumerate(inputs) if t in ("text", "search")]
                if len(text_inputs) == 1:
                    kw_idx = text_inputs[0]
                    score += 1
            if re.search(r"search|so\.|sousuo|/so/|e/search", action, re.I):
                score += 2
            if kw_idx >= 0:
                forms.append((score, action, method, inputs, kw_idx))
        forms.sort(key=lambda x: -x[0])
        return forms

    # ---------- 构造并验证一次搜索 ----------
    @staticmethod
    def _fill(template, encoded_kw):
        return template.replace("%s", encoded_kw)

    def try_search(self, method, url_tpl, param_tpl, charset, kw_title):
        """charset: 'utf-8' / 'gbk'。返回响应或 None。"""
        kw = quote(kw_title.encode(charset, errors="ignore"))
        url = self._fill(url_tpl, kw)
        data = None
        if method == "POST" and param_tpl:
            data = self._fill(param_tpl, kw).encode("ascii")
        # sprintf 安全检查：模板里除 %s 外不能有 %
        if "%" in url_tpl.replace("%s", ""):
            return None
        r = self.f.post(url, data=data, referer=self.root) if method == "POST" \
            else self.f.get(url, referer=self.root)
        if r is None:
            return None
        doc = parse_doc(r)
        if doc is None:
            return None
        page = doc_text(doc)
        html = r.content
        hit_text = kw_title in page
        hit_url = False
        try:
            path = urlparse(self.book_url).path
            if path and path != "/" and path.encode() in html:
                hit_url = True
        except Exception:
            pass
        if hit_text or hit_url:
            return r
        return None

    # ---------- 从搜索结果页反推 xpath ----------
    @staticmethod
    def _steps(a):
        """链接从根开始的路径 [(tag, pos或None), ...]"""
        steps = []
        cur = a
        while cur is not None and isinstance(cur.tag, str):
            p = cur.getparent()
            pos = None
            if p is not None:
                same = [s for s in p if isinstance(s.tag, str) and s.tag == cur.tag]
                if len(same) > 1:
                    pos = same.index(cur) + 1
            steps.append((cur.tag.lower(), pos))
            cur = p
        return steps[::-1]

    def derive_rows(self, doc, probe):
        """从多结果搜索页推导结果行 xpath：
        候选 = 文本含泛化词 或 href 与本书同路径前缀的链接；
        取最大同构组，去掉行号位置谓词。"""
        book_path = urlparse(self.book_url).path
        seg = book_path.strip("/").split("/")[0]
        # pksge 式 /{拼音}/ 书址没有共享前缀，只有 /book/ 式才启用前缀匹配
        prefix = "/" + seg + "/" if seg and "." not in seg else None

        cands = []
        for a in doc.iter("a"):
            href = a.get("href") or ""
            if not href or href.startswith(("#", "javascript")):
                continue
            if probe and probe in norm_space(a.text_content()):
                cands.append(a)
                continue
            if prefix:
                ap = urlparse(urljoin(self.book_url, href))
                if ap.netloc == urlparse(self.book_url).netloc and ap.path.startswith(prefix):
                    cands.append(a)
        if not cands:
            return None
        groups = defaultdict(list)
        for a in cands:
            sig = tuple(t for t, _ in self._steps(a)[-4:])
            groups[sig].append(a)
        best = max(groups.values(), key=len)
        if len(best) < 3:
            return None
        # 逐层比较路径：tag 必须一致；找到位置不同的那一层（行号层），去掉位置谓词
        paths = [self._steps(a) for a in best]
        depth = min(len(p) for p in paths)
        gen, diverged = [], False
        for i in range(depth):
            tags = {p[i][0] for p in paths}
            poss = {p[i][1] for p in paths}
            if len(tags) != 1:
                return None
            tag = tags.pop()
            if len(poss) > 1:
                if diverged:
                    return None  # 只能有一层行号差异
                diverged = True
                gen.append(tag)
            else:
                pos = poss.pop()
                gen.append("%s[%d]" % (tag, pos) if pos else tag)
        if not diverged:
            return None
        xp = "/" + "/".join(gen)
        try:
            if len(doc.xpath(xp)) < 3:
                return None
        except Exception:
            return None
        return xp

    def locate_book(self, doc, page_url, name_xp, kw_title):
        """校验书名搜索结果页里能通过 name_xp 找到本书。"""
        target = norm_url(self.book_url)
        try:
            rows = doc.xpath(name_xp)
        except Exception:
            return False
        for a in rows:
            href = a.get("href") or ""
            if norm_url(urljoin(page_url, href)) == target:
                return True
            if any(t in norm_space(a.text_content()) for t in self.titles):
                return True
        return False

    def derive_author_xpath(self, doc, name_xp, author):
        if not author:
            return ""
        try:
            names = doc.xpath(name_xp)
        except Exception:
            return ""
        if not names:
            return ""
        row = names[0]
        container = row
        for _ in range(4):  # 向上找包含作者信息的行容器
            if container.getparent() is None:
                break
            container = container.getparent()
            if author in container.text_content():
                break
        if author not in container.text_content():
            return ""
        # 在行容器内找包含作者的最深元素
        cand = None
        for el in container.iter():
            if not isinstance(el.tag, str) or el.tag.lower() == "a":
                continue
            if author in (el.text_content() or ""):
                if cand is None or len(el.text_content()) < len(cand.text_content()):
                    cand = el
        if cand is None:
            return ""
        rel = rel_tag_path(container, cand)
        if rel is None:
            return ""
        base = unique_xpath(container, doc)
        xp = base + ("/" + rel if rel else "")
        try:
            got = doc.xpath(xp)
            if len(got) != len(names):  # 数量需与书名一致
                return ""
        except Exception:
            return ""
        return xp

    # ---------- 主流程 ----------
    def detect(self):
        forms = self.find_forms()
        for kw_title in self.titles:
            # 1) 首页表单
            for score, action, method, inputs, kw_idx in forms:
                params = []
                ok = True
                for i, (name, val, _t) in enumerate(inputs):
                    if i == kw_idx:
                        params.append((name, "%s"))
                    else:
                        # 固定参数：只允许安全字符，避免 sprintf 的 % 事故
                        if val and not re.fullmatch(r"[A-Za-z0-9._\-]*", val):
                            ok = False
                            break
                        params.append((name, val))
                if not ok:
                    continue
                query = "&".join("%s=%s" % (k, v) for k, v in params)
                sep = "&" if "?" in action else "?"
                # 声明的方法优先；部分站点表单写 POST 但后端只认 GET（或反之），都试
                if method == "post":
                    attempts = [("POST", action, query), ("GET", action + sep + query, None)]
                else:
                    attempts = [("GET", action + sep + query, None), ("POST", action, query)]
                for m2, url_tpl, param_tpl in attempts:
                    for charset in ("utf-8", "gbk"):
                        r = self.try_search(m2, url_tpl, param_tpl, charset, kw_title)
                        if r is not None:
                            cfg = self._finish(m2, url_tpl, param_tpl, charset, r, kw_title)
                            if cfg:
                                return cfg
            # 2) 常见端点兜底
            for method, path, param_tpl in COMMON_SEARCH:
                url_tpl = urljoin(self.root, path)
                for charset in ("utf-8", "gbk"):
                    r = self.try_search(method, url_tpl, param_tpl, charset, kw_title)
                    if r is not None:
                        cfg = self._finish(method, url_tpl, param_tpl, charset, r, kw_title)
                        if cfg:
                            return cfg
        return None

    def _finish(self, method, url_tpl, param_tpl, charset, resp, kw_title):
        """搜索端点验证通过后：用泛化词再搜一次，从多结果页反推行 xpath。"""
        probe = kw_title[:2] if len(kw_title) >= 2 else kw_title
        kw = quote(probe.encode(charset, errors="ignore"))
        url = self._fill(url_tpl, kw)
        data = None
        if method == "POST" and param_tpl:
            data = self._fill(param_tpl, kw).encode("ascii")
        r2 = self.f.post(url, data=data, referer=self.root) if method == "POST" \
            else self.f.get(url, referer=self.root)
        if r2 is None:
            return None
        doc2 = parse_doc(r2)
        if doc2 is None:
            return None
        name_xp = self.derive_rows(doc2, probe)
        if name_xp is None:
            return None
        # 在书名搜索页上确认能定位到本书（若被重定向到书页本身，视为站方直达，跳过校验）
        doc1 = parse_doc(resp)
        findable = True
        if doc1 is not None and norm_url(resp.url) != norm_url(self.book_url):
            findable = self.locate_book(doc1, resp.url, name_xp, kw_title)
        if not findable:
            return None  # 生成的 xpath 在书名搜索页找不到本书，换下一个端点
        return {
            "method": method,
            "query_url": url_tpl,
            "query_params": param_tpl or "",
            "charset": 1 if charset == "utf-8" else 2,
            "name_xpath": name_xp,
            "mainpage_xpath": name_xp + "/@href",
            "result_url": resp.url,
            "findable": findable,
            "doc_search": doc1,
        }


def build_single_book_search(fetcher, book_url, title):
    """单书模式：站点搜索不可用时，把"搜索"指向本书书页本身。
    搜任何关键词都返回这本书——Reader 只需一条可点的搜索结果即可入库。"""
    r = fetcher.get(book_url)
    if r is None:
        return None
    doc = parse_doc(r)
    if doc is None:
        return None
    page_url = r.url

    # 书名 xpath：h1 优先，要求恰好匹配1次
    name_xp = None
    h1 = doc.xpath("//h1")
    if h1 and norm_space(h1[0].text_content()):
        cand = unique_xpath(h1[0], doc)
        try:
            if len(doc.xpath(cand)) == 1:
                name_xp = cand
        except Exception:
            pass
    if name_xp is None:
        return None

    # 书页自指链接 xpath：canonical 或 指向本页的锚点（文本含书名/主站/开始阅读）
    # 优先与当前页完全同址的链接，其次才是 index.html 归一化后同址的链接
    main_xp, main_url = None, None
    target = norm_url(page_url)
    canon = doc.xpath('//link[@rel="canonical"]/@href')
    if canon and norm_url(urljoin(page_url, canon[0])) == target:
        main_xp = '//link[@rel="canonical"]/@href'
        main_url = urljoin(page_url, canon[0])
    if main_xp is None:
        for strict in (True, False):
            for a in doc.iter("a"):
                href = a.get("href") or ""
                if not href or href.startswith(("#", "javascript")):
                    continue
                full = urljoin(page_url, href)
                same = (full.rstrip("/") == page_url.rstrip("/")) if strict \
                    else (norm_url(full) == target)
                if not same:
                    continue
                text = norm_space(a.text_content())
                if not (title and title in text) and text not in ("主站", "开始阅读", "立即阅读", "查看目录", "目录"):
                    continue
                for xp in (unique_xpath(a, doc), abs_path(a)):
                    try:
                        if len(doc.xpath(xp)) == 1:
                            main_xp, main_url = xp + "/@href", full
                            break
                    except Exception:
                        continue
                if main_xp:
                    break
            if main_xp:
                break
    if main_xp is None:
        return None

    return {
        "method": "GET",
        "query_url": page_url,   # 无 %s：sprintf 原样保留
        "query_params": "",
        "charset": 1,
        "name_xpath": name_xp,
        "mainpage_xpath": main_xp,
        "mainpage_url": main_url,
        "result_url": page_url,
        "findable": True,
        "doc_search": doc,
        "single": True,
    }


def is_waf_page(content):
    """检测 Cloudflare/WAF 挑战页，以及"JS 设置 Cookie 后刷新"的轻量验证墙
    （如 ge_js_validator：响应极小、无正文、靠 document.cookie+reload 放行）。"""
    try:
        text = content[:8192].decode("utf-8", errors="ignore").lower()
    except Exception:
        return False
    if any(m in text for m in WAF_MARKERS):
        return True
    if len(content) < 2048 and "document.cookie" in text \
            and ("location.reload" in text or "location.href" in text) \
            and "<h1" not in text:
        return True
    return False


def reader_preflight(src, list_url, chapter_url):
    """Reader 仿真预检：用 Reader.exe 的真实 UA、不带 Cookie、不跟随跳转，
    复验目录页和章节页。配置正确但 Reader 读不了（如 CF 挑战）的情况在此暴露。"""
    s = requests.Session()
    s.trust_env = False  # 同上：绕过系统代理
    s.headers.update({"User-Agent": READER_UA})
    problems = []
    for label, url, xp in (("目录页", list_url, src["chapter_title_xpath"]),
                           ("章节页", chapter_url, src["content_xpath"])):
        try:
            r = s.get(url, timeout=15, allow_redirects=True)  # 301/302 视为可跟随
        except requests.RequestException as e:
            problems.append("%s 请求失败(%s)" % (label, type(e).__name__))
            continue
        if r.status_code != 200:
            problems.append("%s 返回 HTTP %d" % (label, r.status_code))
            continue
        if is_waf_page(r.content):
            problems.append("%s 被 WAF/Cloudflare 挑战拦截" % label)
            continue
        doc = parse_doc(r)
        if doc is None:
            problems.append("%s 解析失败" % label)
            continue
        try:
            got = doc.xpath(xp)
        except Exception:
            got = []
        if not got:
            problems.append("%s 规则在 Reader UA 下匹配为空" % label)
    return (not problems), "；".join(problems)


def verify_source(bs_file, book_name, debug=False):
    """模拟 Reader 的阅读流程校验书源：搜索 -> 书页 -> 章节列表 -> 正文。"""
    data = json.load(open(bs_file, encoding="utf-8"))
    f = Fetcher(debug=debug)
    ok_all = True
    for src in data.get("book_sources", []):
        print("书源: %s (%s)" % (src.get("title"), src.get("host")))
        # 1. 搜索
        charset = "utf-8" if src.get("query_charset") == 1 else \
                  ("gbk" if src.get("query_charset") == 2 else "utf-8")
        kw = quote(book_name.encode(charset, errors="ignore"))
        if src.get("query_method") == 1:
            url = src["query_url"]
            body = src.get("query_params", "").replace("%s", kw).encode("ascii")
            r = f.post(url, data=body, referer=src["host"])
        else:
            url = src["query_url"].replace("%s", kw)
            r = f.get(url, referer=src["host"])
        if r is None:
            print("  ✗ 搜索请求失败"); ok_all = False; continue
        doc = parse_doc(r)
        if doc is None:
            print("  ✗ 搜索页解析失败"); ok_all = False; continue
        names = doc.xpath(src["book_name_xpath"]) if src.get("book_name_xpath") else []
        urls = doc.xpath(src["book_mainpage_xpath"]) if src.get("book_mainpage_xpath") else []
        if not names or len(names) != len(urls):
            print("  ✗ 搜索结果解析失败 (name=%d, url=%d)" % (len(names), len(urls)))
            ok_all = False; continue
        book_url, hit = None, False
        key = re.sub(r"\s+", "", book_name)
        for n, u in zip(names, urls):
            if key and key in re.sub(r"\s+", "", n.text_content()):
                book_url, hit = urljoin(r.url, u), True
                break
        if not hit:
            book_url = urljoin(r.url, urls[0])
            print("  ⚠ 结果中未找到《%s》，取第一条: %s" % (book_name, norm_space(names[0].text_content())))
        print("  ✓ 搜索: %d 条结果，书页 %s" % (len(names), book_url))

        # 2. 章节列表（必要时经书籍主页跳转）
        r = f.get(book_url)
        if r is None:
            print("  ✗ 书籍主页打开失败"); ok_all = False; continue
        doc = parse_doc(r)
        page_url = r.url
        if src.get("enable_chapter_page") and src.get("chapter_page_xpath"):
            hop = doc.xpath(src["chapter_page_xpath"])
            if not hop:
                print("  ✗ 章节列表页链接解析失败"); ok_all = False; continue
            r = f.get(urljoin(page_url, hop[0]), referer=page_url)
            if r is None:
                print("  ✗ 章节列表页打开失败"); ok_all = False; continue
            doc = parse_doc(r)
            page_url = r.url
        titles = doc.xpath(src["chapter_title_xpath"])
        curls = doc.xpath(src["chapter_url_xpath"])
        if not titles or len(titles) != len(curls):
            print("  ✗ 章节列表解析失败 (title=%d, url=%d)" % (len(titles), len(curls)))
            ok_all = False; continue
        print("  ✓ 章节列表: %d 章，首章《%s》" % (len(titles), norm_space(titles[0].text_content())))

        # 3. 正文
        r = f.get(urljoin(page_url, curls[0]), referer=page_url)
        if r is None:
            print("  ✗ 章节页打开失败"); ok_all = False; continue
        doc = parse_doc(r)
        contents = doc.xpath(src["content_xpath"])
        if len(contents) != 1:
            print("  ✗ 正文匹配数=%d (必须恰好为1)" % len(contents)); ok_all = False; continue
        text = norm_space(contents[0].text_content())
        if len(text) < 100:
            print("  ✗ 正文过短 (%d 字)，疑似识别错误" % len(text)); ok_all = False; continue
        print("  ✓ 正文: %d 字，开头「%s...」" % (len(text), text[:30]))
    print("=" * 56)
    print("校验结果:", "全部通过 ✓" if ok_all else "存在失败项 ✗")
    return ok_all


def extract_content_text(container):
    """模拟 Reader 的正文提取：<br> 与块级结束标签 -> 换行 + xmlNodeGetContent，并去水印。"""
    import html as ihtml
    raw = lxml.html.tostring(container, encoding="unicode")
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    # 块级元素的结束标签同样是分段边界（<p>段落</p> 的站在无此规则时全文拼成一行）
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|blockquote)>", "\n", raw)
    raw = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", raw)
    text = re.sub(r"<[^>]+>", "", raw)
    text = ihtml.unescape(text)
    # 二次解码数字实体：镜像站 CMS 常把上游的实体再转义一层（&amp;#60920;），
    # 站方自己页面也显示 "&#60920;" 乱码——码点解出后多是私用区/零宽字符
    text = re.sub(r"&#(\d{1,7});",
                  lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else "",
                  text)
    text = re.sub(r"&#x([0-9a-fA-F]{1,6});",
                  lambda m: chr(int(m.group(1), 16))
                  if int(m.group(1), 16) < 0x110000 else "", text)
    # 零宽字符与私用区字符（自定义字体映射/反爬插入）在中文正文里都是噪音
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff\ue000-\uf8ff]", "", text)
    text = text.replace("\xa0", "")  # 站点用不间断空格做缩进，去掉（段落换行已保留）
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [_cut_nav_prefix(ln) for ln in lines]
    lines = [ln for ln in lines
             if ln and not is_watermark_line(ln) and not _is_js_text(ln)]
    return "\n".join(lines)


_NAV_WORDS = re.compile(r"(上一章|下一章|章节目录|保存书签|返回目录|加入书签|上一页|下一页)")


def _cut_nav_prefix(line):
    """导航条与正文同行（无标签分隔）时，从最后一个导航词后切开，
    丢弃前面的标题+翻页按钮部分。行内 >=2 处导航词且切后仍有正文才动手。"""
    hits = list(_NAV_WORDS.finditer(line))
    if len(hits) < 2:
        return line
    cut = hits[-1].end()
    rest = line[cut:].lstrip()
    if len(rest) >= 20:  # 切后还有实质内容才认为这行是"导航+正文"混合
        return rest
    return line


# 常见防阅读器水印行（整行判定，保守规则避免误伤正文）
WATERMARK_LINE = [
    re.compile(r"👉"),                       # sharpbro 系：👉👉 当前浏览器转码失败...
    re.compile(r"当前浏览器转码失败"),
    re.compile(r"请退出.{0,4}阅读模式"),
    re.compile(r"退出.{0,2}阅.{0,2}读.{0,2}模.{0,2}式"),
    re.compile(r"^[^。！？]{0,30}本书作者.{0,10}提醒您"),  # ❚本书作者xx提醒您《..
    re.compile(r"#.*#.*#"),                   # 「如章节缺失请退#出#阅#读#模#式」式分行水印
    re.compile(r"^温馨提示[:：]"),            # 按键提示等
    re.compile(r"本章未完|点击下一页|下一页继续阅读"),      # 站方正文分页提示
    re.compile(r"^[没有了目录设置书签分享打赏]{1,12}$"),   # 章尾散碎导航词行
    re.compile(r"一秒记住"),                  # qbxs 系站名水印常用语
    re.compile(r"作品编号|收藏到书柜|点击榜|推荐票|求收藏|求月票"),  # 章首书籍信息块
    re.compile(r"^.{0,50}>.*(?:手机访问|加入书架|章节目录|小说详情|返回)"),  # 站内面包屑头
    re.compile(r".{0,20}最快更新.{0,50}$"),      # 以沫系：站名+最快更新首行水印
    re.compile(r"扫描二维码|手机浏览器"),              # yqyp 系：手机扫码提示头
    re.compile(r"记住.{0,10}域名|朋友.{0,6}书荒|看最新章节.{0,3}完整章节"),  # wanwx 系：防盗截断尾水印
    re.compile(r"^\d{1,4}$"),              # 章首裸编号行（001/002 式页面装饰）
    re.compile(r"剧情升级|故事走向更值得期待"),   # boshishuwu 系：全站固定 AI 导读语
    re.compile(r"请收藏本站|手机版[:：]?[a-z0-9.]{4,}$"),  # nana00 系：章尾收藏提示
]


def is_watermark_line(line):
    if len(line) > 200:
        return False  # 水印行都是短行；超长行几乎必是正文（防分段失效时误杀全文）
    if any(p.search(line) for p in WATERMARK_LINE):
        return True
    # 短行内出现 >=3 个导航词 = 标题+翻页按钮混合的导航条（正文不可能）
    hits = list(_NAV_WORDS.finditer(line))
    return len(hits) >= 3


def apply_filter(text, ftype, keyword):
    """应用书源的文本过滤规则（1=关键字 2=正则）。"""
    if ftype == 1 and keyword:
        return text.replace(keyword, "")
    if ftype == 2 and keyword:
        try:
            return re.sub(keyword, "", text)
        except Exception:
            return text
    return text


def download_book(bs_file, book_url, out_path, limit=0, debug=False):
    """按书源配置抓取整本书，输出 txt。支持断点续抓（.state 文件）。"""
    data = json.load(open(bs_file, encoding="utf-8"))
    src = data["book_sources"][0]
    f = Fetcher(debug=debug)

    # ---- 定位目录页 ----
    main_page = book_url
    if not main_page:
        if "%s" in src.get("query_url", ""):
            log("FAIL", "该书源不是单书模式，请同时提供书籍主页 URL：autobs.py --download bs.json <书页URL>")
            return False
        # 单书模式：query_url 是书页，book_mainpage_xpath 指向真正的书籍主页
        r0 = f.get(src["query_url"])
        if r0 is None:
            log("FAIL", "无法打开书页: %s" % src["query_url"])
            return False
        d0 = parse_doc(r0)
        mp = d0.xpath(src.get("book_mainpage_xpath", " ")) if d0 is not None else []
        if not mp:
            log("FAIL", "书页自指链接解析失败")
            return False
        main_page = urljoin(r0.url, mp[0])
    r = f.get(main_page)
    if r is None:
        log("FAIL", "无法打开书籍主页: %s" % main_page)
        return False
    doc = parse_doc(r)
    page_url = r.url
    if src.get("enable_chapter_page") and src.get("chapter_page_xpath"):
        hop = doc.xpath(src["chapter_page_xpath"])
        if not hop:
            log("FAIL", "章节列表页链接解析失败")
            return False
        r = f.get(urljoin(page_url, hop[0]), referer=page_url)
        if r is None:
            log("FAIL", "无法打开目录页")
            return False
        doc = parse_doc(r)
        page_url = r.url

    # ---- 章节列表（含列表分页） ----
    titles, urls = [], []
    seen_pages = set()
    while True:
        ts = doc.xpath(src["chapter_title_xpath"])
        us = doc.xpath(src["chapter_url_xpath"])
        if not ts or len(ts) != len(us):
            log("FAIL", "章节列表解析失败 (title=%d, url=%d)" % (len(ts), len(us)))
            return False
        for t, u in zip(ts, us):
            titles.append(norm_space(t.text_content()))
            urls.append(urljoin(page_url, u))
        if not src.get("enable_chapter_next"):
            break
        nxt_u = doc.xpath(src.get("chapter_next_url_xpath", " "))
        nxt_k = doc.xpath(src.get("chapter_next_keyword_xpath", " "))
        kw = src.get("chapter_next_keyword", "")
        if not (nxt_u and nxt_k and kw and norm_space(nxt_k[0] if isinstance(nxt_k[0], str) else nxt_k[0].text_content()) in kw):
            break
        nxt_url = urljoin(page_url, nxt_u[0] if isinstance(nxt_u[0], str) else nxt_u[0].get("href"))
        if norm_url(nxt_url) in seen_pages:
            break
        seen_pages.add(norm_url(nxt_url))
        r = f.get(nxt_url, referer=page_url)
        if r is None:
            break
        doc = parse_doc(r)
        page_url = r.url

    total = len(urls)
    log("INFO", "共 %d 章" % total)

    # ---- 断点续抓 ----
    state_file = out_path + ".state"
    done = set()
    if os.path.exists(state_file):
        try:
            done = set(json.load(open(state_file, encoding="utf-8")))
        except Exception:
            done = set()
    new_file = not os.path.exists(out_path) or not done

    ckw = src.get("content_next_keyword", "")
    fetched = 0
    with open(out_path, "a" if not new_file else "w", encoding="utf-8-sig") as fp:
        if new_file:
            fp.write(src.get("title", "") + "\n\n")
        for i, (t, u) in enumerate(zip(titles, urls)):
            if u in done:
                continue
            if limit and fetched >= limit:
                break
            # 抓一章（含正文分页）
            parts = []
            cu, seen = u, set()
            while True:
                rr = f.get(cu, referer=page_url)
                if rr is None:
                    break
                dd = parse_doc(rr)
                if dd is None:
                    break
                got = dd.xpath(src["content_xpath"])
                if len(got) != 1:
                    break
                parts.append(extract_content_text(got[0]))
                if not src.get("enable_content_next"):
                    break
                nu = dd.xpath(src.get("content_next_url_xpath", " "))
                nk = dd.xpath(src.get("content_next_keyword_xpath", " "))
                ktxt = norm_space(nk[0] if nk and isinstance(nk[0], str)
                                  else (nk[0].text_content() if nk else ""))
                if not (nu and ckw and ktxt in ckw):
                    break
                nurl = nu[0] if isinstance(nu[0], str) else nu[0].get("href")
                cu = urljoin(rr.url, nurl)
                if norm_url(cu) in seen or len(seen) >= 8:
                    break
                seen.add(norm_url(cu))
            text = "\n".join(p for p in parts if p)
            text = apply_filter(text, src.get("content_filter_type", 0),
                                src.get("content_filter_keyword", ""))
            if len(text) < 50:
                log("WARN", "第%d章《%s》正文为空或过短，跳过（%s）" % (i + 1, t, u))
                continue
            fp.write(t + "\n" + text + "\n\n")
            fp.flush()
            done.add(u)
            fetched += 1
            if fetched % 20 == 0 or fetched == 1:
                log("INFO", "进度: 已抓 %d 章（当前第%d章 %s）" % (fetched, i + 1, t))
            # 保存进度
            json.dump(sorted(done), open(state_file, "w", encoding="utf-8"))
    log("OK", "本次抓取 %d 章，累计完成 %d/%d 章 -> %s" % (fetched, len(done), total, out_path))
    return True


def drop_latest_block(items):
    """目录页顶部「最新章节」倒序小部件去重。

    很多站点目录页上方有一块"最新 N 章"（倒序），下方才是完整正序列表。
    两种形态：
    1. 同页镜像（wanwx）：小部件的 URL 在下方列表里重现 —— 按 URL 去重丢弃前缀；
    2. 跨页镜像（boshishuwu）：小部件预告的是分页后段的章节，本页 URL 不重现，
       特征 = 前缀章号严格递减 + 其余整体递增。
    返回 (items, dropped)。items: [{"title","url"},...]（原地不改）。"""
    urls = [c["url"] for c in items]
    n = len(urls)
    if len(set(urls)) != n:
        pos = {}
        for i, u in enumerate(urls):
            pos.setdefault(u, []).append(i)
        # 最小 k = 重复 URL 倒数第二次出现位置的最大值 + 1（保证剩余唯一）
        k = 1 + max(p[-2] for p in pos.values() if len(p) >= 2)
        if 0 < k < n:
            tail = set(urls[k:])
            mirror = sum(1 for u in urls[:k] if u in tail)
            if mirror >= k * 0.8:
                return items[k:], k
    # 形态2：前缀章号递减 + 尾部递增（跨页镜像的最新预览块）。
    # 预览块展示的是"最新章节"，其章号必然全部大于本页正序列表的最大章号
    # ——用这一条消解「预览末章 → 第1章」同样递减的边界歧义（大 k 优先）。
    nums = [_chap_num(c.get("title") or "") for c in items]
    if n >= 20:
        for k in range(n // 3, 2, -1):
            head, tailn = nums[:k], [x for x in nums[k:] if x]
            if any(x is None for x in head) or len(tailn) < 10:
                continue
            hp = [(a, b) for a, b in zip(head, head[1:])]
            if (min(head) <= max(tailn)
                    or sum(1 for a, b in hp if a > b) / len(hp) < 0.9):
                continue
            tp = list(zip(tailn, tailn[1:]))
            if sum(1 for a, b in tp if a < b) / len(tp) >= 0.95:
                return items[k:], k
    return items, 0


def resolve_book(url, title=None, author=None, name=None, no_search=False,
                 debug=False, fetcher=None):
    """解析一本书的站点结构，返回结构化结果（供 CLI 与 bookhub 共用）。
    返回 dict：成功时含 src/chapters/pf_ok 等；失败时含 error。"""
    f = fetcher if fetcher is not None else Fetcher(debug=debug)
    report = []

    # ---------- 1. 抓取书籍目录页 ----------
    book_url = url
    r = f.get(book_url)
    if r is None:
        return {"error": "无法打开书籍页面: %s" % book_url}
    book_url = r.url  # 跟随重定向后的最终 URL
    doc = parse_doc(r)
    if doc is None:
        return {"error": "页面解析失败"}
    parsed = urlparse(book_url)
    root = "%s://%s" % (parsed.scheme, parsed.netloc)

    title, author = extract_book_info(doc, book_url)
    title = title or None
    author = author or None
    if not title:
        log("WARN", "未能自动提取书名，搜索探测可能需要 title 参数")
    log("INFO", "书名: %s  作者: %s" % (title or "?", author or "?"))

    # ---------- 2. 章节列表 ----------
    def find_chapter_pair(page_doc):
        for _, items in group_anchors(page_doc, min_count=5):
            pair = build_list_xpath(items, page_doc)
            if pair:
                return pair
        for _, items in group_anchors(page_doc, min_count=3):
            pair = build_list_xpath(items, page_doc)
            if pair:
                return pair
        return None

    def hop_links(page_doc):
        """页面上指向完整目录页的跳转链接（跳过 javascript: 伪链接）。"""
        out = []
        for a in page_doc.iter("a"):
            href = a.get("href") or ""
            if norm_space(a.text_content()) in HOP_TEXTS \
                    and href and not href.startswith(("#", "javascript")):
                out.append(a)
        return out

    chapter_pair = find_chapter_pair(doc)
    hop_xp = ""
    list_url = book_url

    if chapter_pair is None:
        # 当前页没有章节列表：跟随"开始阅读/目录"跳转一次
        for a in hop_links(doc):
            r2 = f.get(urljoin(book_url, a.get("href")), referer=book_url)
            if r2 is None:
                continue
            doc2 = parse_doc(r2)
            if doc2 is None:
                continue
            chapter_pair = find_chapter_pair(doc2)
            if chapter_pair:
                hop_xp = abs_path(a) + "/@href"
                doc = doc2
                list_url = r2.url
                break
    else:
        # 章节数过少：可能只是"最新章节"块，尝试跳转到完整目录页换取更全的列表
        n_now = len(doc.xpath(chapter_pair[0]))
        if n_now < 30:
            for a in hop_links(doc):
                r2 = f.get(urljoin(book_url, a.get("href")), referer=book_url)
                if r2 is None:
                    continue
                doc2 = parse_doc(r2)
                if doc2 is None:
                    continue
                pair2 = find_chapter_pair(doc2)
                if pair2 and len(doc2.xpath(pair2[0])) > n_now:
                    log("INFO", "检测到更完整的目录页（%d → %d 章）" %
                        (n_now, len(doc2.xpath(pair2[0]))))
                    chapter_pair = pair2
                    hop_xp = abs_path(a) + "/@href"
                    doc = doc2
                    list_url = r2.url
                    break

    if chapter_pair is None:
        return {"error": "未能识别章节列表，请确认输入的是书籍目录页"}

    title_xp, url_xp = chapter_pair
    chapters_el = doc.xpath(title_xp)
    # href 首尾空白清洗：部分站模板输出 "xxx.html " 带尾随空格，
    # requests 会编码成 %20 导致 404（boshishuwu 实测 25/108 条中招）
    chapter_hrefs = [h.strip() for h in doc.xpath(url_xp)]
    chapters = [{"title": norm_space(e.text_content()),
                 "url": urljoin(list_url, h)}
                for e, h in zip(chapters_el, chapter_hrefs)]
    chapters, dropped = drop_latest_block(chapters)
    if dropped:
        chapters_el = chapters_el[dropped:]
        chapter_hrefs = chapter_hrefs[dropped:]
    n_ch = len(chapters)
    sample = norm_space(chapters_el[0].text_content()) if n_ch else ""
    log("OK", "章节列表: %d 章  首章: %s%s" %
        (n_ch, sample, "（已去除顶部最新章节镜像块 %d 条）" % dropped if dropped else ""))
    report.append(("章节列表", True, "%d 章" % n_ch))
    if n_ch < 10:
        log("WARN", "章节数偏少，若是短书正常；否则可能识别到了错误区块")

    # ---------- 3. 章节列表分页 ----------
    ch_next = detect_chapter_pagination(f, list_url, title_xp, chapter_hrefs)
    enable_ch_next = 1 if ch_next else 0
    if ch_next:
        log("OK", "检测到章节列表分页")
        report.append(("章节列表分页", True, ""))

    # ---------- 4. 正文 ----------
    first_abs = urljoin(list_url, chapter_hrefs[0])
    mid_abs = urljoin(list_url, chapter_hrefs[n_ch // 2]) if n_ch > 2 else first_abs

    # 分页合并：网关/txt 下载需要完整章节列表（Reader 直连仍用 next xpath 自翻页）。
    # 每页先各自 drop_latest_block（各页常带同款"最新章节"预览块），再按 URL 去重拼接。
    if ch_next:
        cur_doc, cur_url, pseen = doc, list_url, {norm_url(list_url)}
        seen = {norm_url(c["url"]) for c in chapters}
        for _ in range(20):
            # 不用 ch_next[0] 的位置 xpath：目录分页后各页按钮数量不同
            # （p2 多"上一页"），同位置会取到页码/回退链接；语义查找更稳
            _el, nu = find_next_page_anchor(cur_doc, cur_url)
            if not nu or norm_url(nu) in pseen:
                break
            pseen.add(norm_url(nu))
            rp = f.get(nu, referer=cur_url)
            if rp is None:
                time.sleep(1.5)
                rp = f.get(nu, referer=cur_url)  # 偶发 404/超时重试
            if rp is None:
                log("WARN", "目录分页 %s 抓取失败，已合并 %d 章" % (nu, len(chapters)))
                break
            dp = parse_doc(rp)
            if dp is None:
                break
            hrefs_p = [h.strip() for h in dp.xpath(url_xp)]
            page_ch = [{"title": norm_space(e.text_content()),
                        "url": urljoin(nu, h)}
                       for e, h in zip(dp.xpath(title_xp), hrefs_p)]
            page_ch, _ = drop_latest_block(page_ch)
            added = 0
            for c in page_ch:
                k2 = norm_url(c["url"])
                if k2 not in seen:
                    seen.add(k2)
                    chapters.append(c)
                    added += 1
            if not added:
                break
            cur_doc, cur_url = dp, nu
        if len(chapters) != n_ch:
            log("OK", "目录分页合并: %d → %d 章" % (n_ch, len(chapters)))
            report.append(("目录分页合并", True, "共 %d 章" % len(chapters)))
            n_ch = len(chapters)

    ch_docs = []
    for u in (first_abs, mid_abs):
        rr = f.get(u, referer=list_url)
        if rr is None:
            time.sleep(1.5)
            rr = f.get(u, referer=list_url)  # 站点偶发 404/超时，单次重试
        if rr is None:
            return {"error": "无法打开章节页: %s" % u}
        d = parse_doc(rr)
        if d is not None:
            ch_docs.append(d)
    content_xp = detect_content_xpath(ch_docs)
    if content_xp is None:
        return {"error": "未能识别正文容器"}
    ctext_len = len(clean_text(ch_docs[0].xpath(content_xp)[0]))
    log("OK", "正文: %s (%d 字)" % (content_xp, ctext_len))
    report.append(("章节正文", True, "%d 字" % ctext_len))

    # ---------- 5. 正文分页 ----------
    enable_ct_next = 0
    ct_next = ("", "")
    nxt_el, _ = find_next_page_anchor(ch_docs[0], first_abs)
    if nxt_el is not None:
        # 该"下一页"必须不像"下一章"（href 不应等于下一章地址）
        next_href_abs = norm_url(urljoin(first_abs, nxt_el.get("href")))
        next_ch_abs = norm_url(urljoin(list_url, chapter_hrefs[1])) if n_ch > 1 else ""
        if next_href_abs != next_ch_abs:
            ok, extra = verify_content_pagination(f, first_abs, content_xp, nxt_el)
            if ok:
                xp = unique_xpath(nxt_el, ch_docs[0])
                enable_ct_next = 1
                ct_next = (xp + "/@href", xp)
                log("OK", "检测到正文分页（续页约 %d 字）" % extra)
                report.append(("正文分页", True, ""))

    # ---------- 6. 搜索 ----------
    search_cfg = None
    single_mode = False
    author_xp = ""
    if not no_search and title:
        log("INFO", "探测站内搜索...")
        det = SearchDetector(f, root, book_url, title)
        search_cfg = det.detect()
        if search_cfg:
            # 在书名搜索结果页上反推作者 xpath
            doc_s = search_cfg.pop("doc_search", None)
            if doc_s is not None and author:
                author_xp = det.derive_author_xpath(doc_s, search_cfg["name_xpath"], author)
            findable = search_cfg.pop("findable", True)
            log("OK", "搜索: %s %s (charset=%s)" % (
                search_cfg["method"], search_cfg["query_url"],
                "UTF-8" if search_cfg["charset"] == 1 else "GBK"))
            if not findable:
                log("WARN", "书名搜索页中未能用生成的 xpath 定位到本书，搜索可能不稳定")
            report.append(("搜索配置", findable, search_cfg["query_url"]))
        else:
            # 站点搜索不可用：退化为"单书模式"，搜索任何词都返回这本书
            search_cfg = build_single_book_search(f, book_url, title)
            if search_cfg:
                single_mode = True
                mainpage_url = search_cfg.pop("mainpage_url", None)
                search_cfg.pop("doc_search", None)
                search_cfg.pop("findable", None)
                search_cfg.pop("single", None)
                # 自指链接若直接指向完整目录页，则无需"书籍主页→目录页"跳转
                def _np(u):
                    p = urlparse(u)
                    return p.netloc.lower() + re.sub(r"/(index\.(html?|shtml|php))?$", "/", p.path)
                if mainpage_url and hop_xp and _np(mainpage_url) == _np(list_url):
                    hop_xp = ""
                    log("INFO", "书页自指链接即目录页，已关闭主页跳转")
                log("OK", "站内搜索不可用，已启用单书模式（搜索任意关键词都只返回本书）")
                report.append(("搜索配置", True, "单书模式"))
            else:
                log("WARN", "未能自动发现可用搜索，导入后需在书源管理里手工补 搜索URL")
                report.append(("搜索配置", False, "需手工补充"))

    # ---------- 7. 站点名 ----------
    src_name = name
    if not src_name and single_mode and title:
        src_name = title  # 单书模式直接用书名做书源名
    if not src_name:
        rh = f.get(root)
        if rh is not None:
            droot = parse_doc(rh)
            if droot is not None:
                # 优先 og:site_name；<title> 只在接受度高的前提下使用
                for m in droot.iter("meta"):
                    prop = (m.get("property") or m.get("name") or "").lower()
                    if prop in ("og:site_name", "application-name"):
                        src_name = norm_space(m.get("content"))
                        break
                if not src_name:
                    t = droot.xpath("//title")
                    if t:
                        cand = norm_space(t[0].text_content())
                        cand = re.split(r"[_\-—|]", cand)[0].strip()
                        # 过滤 SEO 堆砌标题（含逗号/过长/含书名）
                        if cand and len(cand) <= 12 and "，" not in cand and "," not in cand \
                                and (not title or title not in cand):
                            src_name = cand
    if not src_name:
        src_name = parsed.netloc.replace("www.", "").split(".")[0]
    src_name = src_name[:20]

    # ---------- 8. 生成书源 ----------
    src = {
        "title": src_name,
        "host": root,
        "query_url": search_cfg["query_url"] if search_cfg else "",
        "query_method": (1 if search_cfg and search_cfg["method"] == "POST" else 0),
        "query_params": search_cfg["query_params"] if search_cfg else "",
        "query_charset": search_cfg["charset"] if search_cfg else 0,
        "book_name_xpath": search_cfg["name_xpath"] if search_cfg else "",
        "book_mainpage_xpath": search_cfg["mainpage_xpath"] if search_cfg else "",
        "book_author_xpath": author_xp,
        "enable_chapter_page": 1 if hop_xp else 0,
        "chapter_page_xpath": hop_xp,
        "chapter_title_xpath": title_xp,
        "chapter_url_xpath": url_xp,
        "enable_chapter_next": enable_ch_next,
        "chapter_next_url_xpath": ch_next[0] if ch_next else "",
        "chapter_next_keyword_xpath": ch_next[1] if ch_next else "",
        "chapter_next_keyword": NEXT_TEXTS[0] if ch_next else "",
        "content_xpath": content_xp,
        "enable_content_next": enable_ct_next,
        "content_next_url_xpath": ct_next[0],
        "content_next_keyword_xpath": ct_next[1],
        "content_next_keyword": NEXT_TEXTS[0] if enable_ct_next else "",
        "content_filter_type": 0,
        "content_filter_keyword": "",
    }

    # ---------- 9. Reader 仿真预检 ----------
    pf_ok, pf_msg = reader_preflight(src, list_url, first_abs)
    report.append(("Reader仿真", pf_ok, pf_msg))
    if not pf_ok:
        log("WARN", "Reader 仿真预检未通过：%s" % pf_msg)

    return {
        "src": src,
        "title": title or src_name,
        "author": author or "",
        "root": root,
        "book_url": book_url,
        "list_url": list_url,
        "first_chapter_url": first_abs,
        "chapters": chapters,
        "chapter_count": n_ch,
        "search_ok": bool(search_cfg),
        "single_mode": single_mode,
        "reader_ok": pf_ok,
        "reader_msg": pf_msg,
        "report": report,
    }


def main():
    ap = argparse.ArgumentParser(description="Reader 书源自动配置工具")
    ap.add_argument("url", nargs="?", help="书籍目录页 URL")
    ap.add_argument("--verify", metavar="BS_JSON", help="校验已有书源文件，需配合 --book")
    ap.add_argument("--book", help="--verify 时用于搜索的书名")
    ap.add_argument("--download", metavar="BS_JSON", help="按书源配置抓取全书存为 txt（断点续抓）")
    ap.add_argument("--out", help="--download 的输出 txt 路径")
    ap.add_argument("--limit", type=int, default=0, help="--download 最多抓几章（调试用）")
    ap.add_argument("--title", help="书名（默认自动提取）")
    ap.add_argument("--author", help="作者（默认自动提取，仅用于作者xpath）")
    ap.add_argument("--name", help="书源名称（默认用站点标题/域名）")
    ap.add_argument("-o", "--output", help="输出 json 文件路径")
    ap.add_argument("--no-search", action="store_true", help="跳过搜索配置探测")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.verify:
        if not args.book:
            ap.error("--verify 需要配合 --book 指定搜索书名")
        sys.exit(0 if verify_source(args.verify, args.book, args.debug) else 1)
    if args.download:
        out = args.out
        if not out:
            try:
                _d = json.load(open(args.download, encoding="utf-8"))
                _t = _d["book_sources"][0].get("title", "book")
            except Exception:
                _t = "book"
            out = re.sub(r'[\\/:*?"<>|]', "_", _t) + ".txt"
        sys.exit(0 if download_book(args.download, args.url, out,
                                    args.limit, args.debug) else 1)
    if not args.url:
        ap.error("缺少书籍目录页 URL")

    res = resolve_book(args.url, title=args.title, author=args.author,
                       name=args.name, no_search=args.no_search,
                       debug=args.debug)
    if "error" in res:
        log("FAIL", res["error"])
        sys.exit(1)

    src = res["src"]
    report = res["report"]
    search_cfg = res["search_ok"]
    single_mode = res["single_mode"]
    pf_ok, pf_msg = res["reader_ok"], res["reader_msg"]
    src_name = src["title"]

    out = args.output
    if not out:
        out = "bs_%s.json" % re.sub(r"[^\w]+", "_", urlparse(args.url).netloc)
    with open(out, "w", encoding="utf-8") as fp:
        json.dump({"book_sources": [src]}, fp, ensure_ascii=False, indent=2)

    print()
    print("=" * 56)
    print("书源「%s」已生成: %s" % (src_name, out))
    for name_, ok, extra in report:
        print("  %s %-10s %s" % ("✓" if ok else "✗", name_, extra))
    print("=" * 56)
    if not pf_ok:
        print("⚠ 重要：该站点对 Reader 客户端有防护（%s）。" % pf_msg)
        print("  配置本身已生成，但导入 Reader 后很可能无法正常打开书籍，")
        print("  建议换一个无防护的书站重新运行本工具。")
    if not search_cfg:
        print("注意: 搜索配置未自动生成，导入后请在【书源管理】中手工填写")
        print("      【搜索URL】及搜索页解析的三个 xpath，否则无法按书名搜到书。")
    elif single_mode:
        print("注意: 本站搜索不可用，已生成单书书源——在 Reader 里搜索时")
        print("      输入任意关键词都只会列出《%s》这一本书。" % src_name)
    print("导入方法: Reader → 设置 → 在线小说 → 书源管理 → 导入 → 选择该文件")
    if not pf_ok:
        sys.exit(3)
    sys.exit(0 if search_cfg or args.no_search else 2)


if __name__ == "__main__":
    main()
