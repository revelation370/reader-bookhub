# -*- coding: utf-8 -*-
"""网关书库 + Reader 专用 HTML 代理。

对 Reader 展示三个固定结构的页面（UTF-8）：
  /g/search?q=书名   -> 搜索结果页（书名 a.bh-name + 作者 span.bh-author）
  /g/book/{id}       -> 目录页（章节 a.bh-chapter，链接指向 /g/chapter/{id}/{n}）
  /g/chapter/{id}/{n}-> 章节页（正文 div#bh-content，站方分页已在网关侧合并）

书的真实抓取走分层 Fetcher（L1/L2/L3），对 Reader 而言书源永远是本机地址，
Cloudflare 等防护在网关侧被消化。
"""
import hashlib
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "autobs"))
from autobs import (parse_doc, norm_url, norm_space, clean_text,  # noqa: E402
                    extract_content_text, resolve_book, find_next_page_anchor)
from urllib.parse import urljoin

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
BOOKS_DIR = os.path.join(DATA_DIR, "books")   # v2: 一书一文件 data/books/{id}.json
LEGACY_BOOKS_FILE = os.path.join(DATA_DIR, "books.json")  # v1 单文件，启动时自动迁移
CACHE_DIR = os.path.join(DATA_DIR, "cache")
CATALOG_TTL = 1800  # 目录缓存 30 分钟；过期后旧目录秒回 + 后台静默刷新

GW_XPATHS = {
    "book_name_xpath": "//a[@class='bh-name']",
    "book_mainpage_xpath": "//a[@class='bh-name']/@href",
    "book_author_xpath": "//span[@class='bh-author']",
    "chapter_title_xpath": "//a[@class='bh-chapter']",
    "chapter_url_xpath": "//a[@class='bh-chapter']/@href",
    "content_xpath": "//div[@id='bh-content']",
}


def _book_id(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _atomic_write_json(path, obj):
    """原子写 JSON：先写临时文件再替换，进程中途挂掉不会损坏原文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(obj, fp, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _url_hash(url):
    """章节 URL 哈希：作为缓存键。目录刷新后章节序号可能漂移，
    按 URL 命中才能保证不张冠李戴。"""
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:16]


class BookStore:
    """书库 v2：一书一文件（data/books/{id}.json，原子写）。

    - 搜索命中只做内存注册（register），不落盘——书库文件数 = 书架上的书
    - 解析成功或用户加入书架（shelf=True）才持久化
    - 启动时自动把旧版单文件 books.json 迁移为拆分文件（原文件保留 .migrated 备份）
    """

    def __init__(self):
        os.makedirs(BOOKS_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._lock = threading.RLock()
        self._pf_lock = threading.Lock()
        self._pf_active = set()
        self._books = {}
        self._migrate_legacy()
        for fn in os.listdir(BOOKS_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                b = json.load(open(os.path.join(BOOKS_DIR, fn), encoding="utf-8"))
                self._books[b["id"]] = b
            except Exception:
                continue

    def _migrate_legacy(self):
        """v1 单文件 -> v2 一书一文件；同时迁移旧序号命名的章节缓存。"""
        if not os.path.exists(LEGACY_BOOKS_FILE):
            return
        try:
            legacy = json.load(open(LEGACY_BOOKS_FILE, encoding="utf-8"))
        except Exception:
            return
        for bid, b in legacy.items():
            if b.get("shelf") or b.get("resolved"):
                _atomic_write_json(os.path.join(BOOKS_DIR, bid + ".json"), b)
                # 旧缓存是 00001.txt 序号命名 -> 改为 URL 哈希命名
                for i, ch in enumerate(b.get("chapters") or [], 1):
                    old = os.path.join(CACHE_DIR, bid, "%05d.txt" % i)
                    if os.path.exists(old):
                        new = os.path.join(CACHE_DIR, bid, _url_hash(ch.get("url")) + ".txt")
                        try:
                            os.replace(old, new)
                        except Exception:
                            pass
        os.replace(LEGACY_BOOKS_FILE, LEGACY_BOOKS_FILE + ".migrated")

    def _persist(self, bid):
        with self._lock:
            b = self._books.get(bid)
            if b is not None and b.get("shelf"):
                _atomic_write_json(os.path.join(BOOKS_DIR, bid + ".json"), b)

    def register(self, url, title="", author="", site=""):
        """内存注册一本书（仅供网关链接寻址/懒解析），返回 book_id。
        注册 ≠ 书架；不落盘，进程重启即消失（搜索残留无保留价值）。"""
        bid = _book_id(url)
        with self._lock:
            b = self._books.get(bid)
            if b is None:
                b = {"id": bid, "url": url, "title": title, "author": author,
                     "site": site, "resolved": False, "chapters": [],
                     "created": time.time(), "resolved_at": 0, "error": "",
                     "shelf": False}
                self._books[bid] = b
            else:
                if title:
                    b["title"] = title
                if author:
                    b["author"] = author
        return bid

    def add_to_shelf(self, bid):
        with self._lock:
            b = self._books.get(bid)
            if b is not None and not b.get("shelf"):
                b["shelf"] = True
        self._persist(bid)

    def delete(self, bid):
        """移除一本书：内存记录 + 书库文件 + 章节缓存目录。"""
        import shutil
        with self._lock:
            existed = self._books.pop(bid, None) is not None
        try:
            os.remove(os.path.join(BOOKS_DIR, bid + ".json"))
        except OSError:
            pass
        try:
            shutil.rmtree(os.path.join(CACHE_DIR, bid), ignore_errors=True)
        except Exception:
            pass
        return existed

    def clear_cache(self, bid):
        """清空一本书的章节缓存（书架记录保留，下次阅读重新抓取）。"""
        import shutil
        shutil.rmtree(os.path.join(CACHE_DIR, bid), ignore_errors=True)

    def get(self, bid):
        return self._books.get(bid)

    def all(self, shelf_only=True):
        """shelf_only=True 只返回书架上的书（用户主动加入/打开过的）。"""
        with self._lock:
            books = list(self._books.values())
        if shelf_only:
            books = [b for b in books if b.get("shelf")]
        return books

    def get_catalog(self, bid, fetcher):
        """Reader 侧取目录：有旧数据立即返回（哪怕过期），过期则在后台
        静默刷新——绝不让 Reader 的请求同步等待分钟级的重新解析。"""
        with self._lock:
            b = self._books.get(bid)
        if b is None:
            return None, "书不存在"
        if b.get("resolved"):
            stale = time.time() - (b.get("resolved_at") or 0) > CATALOG_TTL
            if stale and not b.get("_refreshing"):
                with self._lock:
                    b["_refreshing"] = True
                threading.Thread(target=self._bg_refresh,
                                 args=(bid, fetcher), daemon=True).start()
            return b, ""
        return self.ensure_resolved(bid, fetcher)

    def _bg_refresh(self, bid, fetcher):
        try:
            # 独立抓取器：解析流程（含搜索探测的大量试错请求）不与
            # Reader 正在读的章节请求共享限速/会话，避免互相拖慢
            try:
                from engine.fetcher import LayeredFetcher
                fetcher = LayeredFetcher()
            except ImportError:
                from fetcher import LayeredFetcher
                fetcher = LayeredFetcher()
            self.ensure_resolved(bid, fetcher, force=True)
        finally:
            with self._lock:
                b = self._books.get(bid)
                if b is not None:
                    b["_refreshing"] = False

    def ensure_resolved(self, bid, fetcher, force=False):
        """确保书的章节列表已解析（懒解析 + TTL）。返回 (book, error)。"""
        with self._lock:
            b = self._books.get(bid)
        if b is None:
            return None, "书不存在"
        fresh = time.time() - (b.get("resolved_at") or 0) < CATALOG_TTL
        if b.get("resolved") and fresh and not force:
            return b, ""
        res = resolve_book(b["url"], title=b.get("title") or None,
                           author=b.get("author") or None, fetcher=fetcher)
        if "error" in res:
            with self._lock:
                b["error"] = res["error"]
            return b, res["error"]
        with self._lock:
            b.update({
                "title": res["title"], "author": res["author"],
                "chapters": res["chapters"], "chapter_count": res["chapter_count"],
                "src": res["src"], "list_url": res["list_url"],
                "reader_ok": res["reader_ok"], "reader_msg": res["reader_msg"],
                "resolved": True, "resolved_at": time.time(), "error": "",
                "shelf": True,  # 解析成功即视为用户在读这本书，自动入书架
            })
        self._persist(bid)
        # 后台尝试沉淀种子（该站站内搜索可用才写入；失败静默——
        # 站点不稳定时下次解析会再试）
        threading.Thread(target=self._try_sediment, args=(bid,), daemon=True).start()
        return b, ""

    def _try_sediment(self, bid):
        try:
            from search import maybe_add_seed
            from fetcher import LayeredFetcher
            b = self.get(bid)
            if not b or not b.get("src"):
                return
            solo = LayeredFetcher()
            got = maybe_add_seed(b["src"], solo)
            if got:
                print("[seed] 沉淀新种子: %s" % got.get("name"))
        except Exception:
            pass

    def fetch_chapter(self, bid, n, fetcher, prefetch=False):
        """抓第 n 章（1-based），返回正文文本（已清洗水印）。失败返回 None。
        缓存键 = 章节 URL 哈希（目录刷新后章节序号会漂移，按 URL 才不会错位）。
        正常阅读（prefetch=False）成功后自动后台预取后续 5 章。"""
        b, err = self.get_catalog(bid, fetcher)
        if err or not b.get("chapters"):
            return None
        if n < 1 or n > len(b["chapters"]):
            return None
        ch = b["chapters"][n - 1]
        d = os.path.join(CACHE_DIR, bid)
        os.makedirs(d, exist_ok=True)
        cpath = os.path.join(d, _url_hash(ch["url"]) + ".txt")
        hit = False
        if os.path.exists(cpath):
            hit = True
            try:
                text = open(cpath, encoding="utf-8").read()
            except Exception:
                hit = False
                text = None
            if hit:
                if not prefetch:
                    self._spawn_prefetch(bid, n, fetcher)
                return text
        src = b.get("src") or {}
        parts = []
        cu, seen, hops = ch["url"], set(), 0
        while cu and hops < 8:
            rr = fetcher.get(cu, referer=b.get("list_url"))
            if rr is None:
                break
            dd = parse_doc(rr)
            if dd is None:
                break
            got = dd.xpath(src.get("content_xpath", ""))
            if len(got) != 1:
                break
            parts.append(extract_content_text(got[0]))
            if not src.get("enable_content_next"):
                break
            nxt, _ = find_next_page_anchor(dd, rr.url)
            if nxt is None:
                break
            nu = urljoin(rr.url, nxt.get("href"))
            if norm_url(nu) in seen or norm_url(nu) == norm_url(cu):
                break
            seen.add(norm_url(cu))
            cu = nu
            hops += 1
        text = "\n".join(p for p in parts if p)
        if len(text) < 50:
            return None
        text = _filter_domain_watermark(text, ch["url"])
        try:
            open(cpath, "w", encoding="utf-8").write(text)
        except Exception:
            pass
        if not prefetch:
            self._spawn_prefetch(bid, n, fetcher)
        return text

    # ---------- 自动预取：读到第 n 章时后台顺手抓 n+1..n+5 ----------
    _PREFETCH_AHEAD = 5

    def _spawn_prefetch(self, bid, n, fetcher):
        """预取入口（防重入：同一本书同一时刻只跑一个预取任务）。"""
        with self._pf_lock:
            if bid in self._pf_active:
                return
            self._pf_active.add(bid)
        threading.Thread(target=self._prefetch_worker,
                         args=(bid, n, fetcher), daemon=True).start()

    def _prefetch_worker(self, bid, n, fetcher):
        try:
            with self._lock:
                b = self._books.get(bid)
                total = len(b.get("chapters") or []) if b else 0
            for i in range(n + 1, min(n + self._PREFETCH_AHEAD, total) + 1):
                try:
                    self.fetch_chapter(bid, i, fetcher, prefetch=True)
                except Exception:
                    pass  # 预取尽力而为，失败不影响阅读
        finally:
            with self._pf_lock:
                self._pf_active.discard(bid)


def _filter_domain_watermark(text, page_url):
    """删除含混淆域名的短行（如「[全本小说]:qbxs. N e t 一秒记住！」——
    站方在正文里插的字符间加空格型站名水印）。
    主域 <4 字符时跳过（如 m.，避免误伤）。"""
    from urllib.parse import urlparse
    host = urlparse(page_url).netloc.lower().replace("www.", "")
    if "." not in host:
        return text
    main = host.split(".")[0]
    if len(main) < 4:
        return text
    out = []
    for line in text.split("\n"):
        if len(line) < 100 and main in re.sub(r"[\s\-_·.。,，:：]+", "", line.lower()):
            continue
        out.append(line)
    return "\n".join(out)


# ---------------- HTML 渲染 ----------------

def render_search_page(results, base):
    """搜索结果页：结构与 GW_XPATHS 对齐。results: [{id,title,author,site}]
    链接带 ?src=站点 —— Reader 的"主页"列由此可辨来源（查询参数不影响路由）。"""
    rows = []
    for r in results:
        src = ("?src=" + (r["site"] or "").replace(" ", "")) if r.get("site") else ""
        rows.append(
            "<div class='bh-result'>"
            "<a class='bh-name' href='%s/g/book/%s%s'>%s</a>"
            "<span class='bh-site'>%s</span>"
            "<span class='bh-author'>%s</span>"
            "</div>" % (base, r["id"], src, _esc(r["title"]), _esc(r["site"] or ""),
                        _esc(r.get("author") or "")))
    body = "\n".join(rows) or "<div class='bh-empty'>没有找到相关书籍</div>"
    return "<html><head><meta charset='utf-8'></head><body>\n%s\n</body></html>" % body


def render_catalog_page(book, base):
    """目录页：章节链接指向 /g/chapter/{id}/{n}。"""
    items = []
    for i, ch in enumerate(book.get("chapters") or [], 1):
        items.append("<a class='bh-chapter' href='%s/g/chapter/%s/%d'>%s</a>"
                     % (base, book["id"], i, _esc(ch["title"])))
    body = "<h1>%s %s</h1>\n%s" % (
        _esc(book.get("title") or ""), _esc(book.get("author") or ""),
        "\n".join(items) or "<p>章节列表为空</p>")
    return "<html><head><meta charset='utf-8'></head><body>\n%s\n</body></html>" % body


def render_chapter_page(title, text):
    """章节页：正文容器 id=bh-content，段落用 <br> 分隔（Reader TidyHtml 会转成换行）。"""
    body = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = body.replace("\n", "<br>")
    return ("<html><head><meta charset='utf-8'></head><body>"
            "<h2>%s</h2><div id='bh-content'>%s</div></body></html>"
            % (_esc(title), body))
