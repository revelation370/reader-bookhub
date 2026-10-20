# -*- coding: utf-8 -*-
"""bookhub 主服务：找书 Web UI + 聚合搜索 + 解析 + txt 导出 + Reader 网关。

启动: python server.py   （默认 http://127.0.0.1:8787）
"""
import json
import os
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "autobs"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from engine.fetcher import LayeredFetcher
from engine.search import aggregate_search
from engine import gateway as gw
from autobs import resolve_book

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 配置 ----------------
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEF_CONFIG = {
    "host": "127.0.0.1",
    "port": 80,  # 必须是 80：Reader 的网络库不支持带端口的 URL
    "reader_dir": os.path.join(BASE_DIR, "books"),  # txt 导出目录，可在 config.json 改
    "headless": True,
}
config = dict(DEF_CONFIG)
if os.path.exists(CONFIG_PATH):
    try:
        config.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
    except Exception:
        pass
else:
    # 首次运行生成配置（已存在则不覆盖用户配置）
    with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
        json.dump(config, fp, ensure_ascii=False, indent=2)

# 运行日志轮转：超 1MB 截断，防止无限增长
LOG_PATH = os.path.join(BASE_DIR, "server.log")
try:
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
        os.replace(LOG_PATH, LOG_PATH + ".old")
except Exception:
    pass

GW_BASE = ("http://%s" % config["host"]) if config["port"] == 80 else ("http://%s:%d" % (config["host"], config["port"]))

app = FastAPI(title="bookhub")
fetcher = LayeredFetcher(debug=False, headless=config.get("headless", True))
store = gw.BookStore()

# ---------------- 后台任务 ----------------
JOBS = {}
JOBS_LOCK = threading.Lock()


def _new_job(kind):
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "status": "running",
                     "progress": 0, "log": [], "result": None, "created": time.time()}
    return JOBS[jid]


def _job_log(job, msg):
    if job is not None:
        job["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
    print("[bookhub] %s" % msg)


def _run_resolve_job(job, bid, url, want_txt):
    try:
        b, err = store.ensure_resolved(bid, fetcher, force=True)
        if err:
            job["status"] = "failed"
            job["result"] = {"error": err}
            return
        job["progress"] = 60
        _job_log(job, "解析完成：%d 章，Reader直连：%s" % (
            b["chapter_count"], "可" if b.get("reader_ok") else "不可（走网关/txt）"))
        out = {"book": _book_info(b), "gateway_url": "%s/g/book/%s" % (GW_BASE, bid),
               "reader_ok": b.get("reader_ok")}
        # 生成独立书源文件（可直连站直接导入 Reader 用）
        bs_path = os.path.join(BASE_DIR, "out", "bs_%s.json" % re.sub(r"[^\w]+", "_", b.get("site") or bid))
        os.makedirs(os.path.dirname(bs_path), exist_ok=True)
        with open(bs_path, "w", encoding="utf-8") as fp:
            json.dump({"book_sources": [b["src"]]}, fp, ensure_ascii=False, indent=2)
        out["bs_file"] = bs_path
        job["result"] = out
        job["progress"] = 80
        # 动态沉淀：新站站内搜索可用则加入种子，下次直搜覆盖
        try:
            from engine.search import maybe_add_seed
            new_seed = maybe_add_seed(b.get("src"), fetcher)
            if new_seed:
                _job_log(job, "已把「%s」加入搜索种子（下次可直接搜到）" % new_seed["name"])
        except Exception:
            pass
        if want_txt:
            txt_path = _export_txt(b, job)
            out["txt_file"] = txt_path
        job["status"] = "done"
        job["progress"] = 100
    except Exception as e:
        job["status"] = "failed"
        job["result"] = {"error": "%s: %s" % (type(e).__name__, e)}


def _export_txt(b, job=None):
    """把已解析的书整本导出 txt（跳过已有缓存，断点续抓）。"""
    safe = re.sub(r'[\\/:*?"<>|]', "_", b["title"] or b["id"])[:60]
    out_path = os.path.join(config["reader_dir"], safe + ".txt")
    os.makedirs(config["reader_dir"], exist_ok=True)
    total = len(b["chapters"])
    done = 0
    with open(out_path, "w", encoding="utf-8-sig") as fp:
        fp.write((b["title"] or "") + ("\n作者：%s" % b["author"] if b.get("author") else "") + "\n\n")
        for i in range(1, total + 1):
            text = store.fetch_chapter(b["id"], i, fetcher)
            ch = b["chapters"][i - 1]
            if not text:
                _job_log(job, "第%d章《%s》抓取失败，跳过" % (i, ch["title"]))
                continue
            fp.write(ch["title"] + "\n" + text + "\n\n")
            done += 1
            if job and (done % 25 == 0 or done == total):
                job["progress"] = 80 + int(20 * done / max(1, total))
                _job_log(job, "txt 进度 %d/%d" % (done, total))
    _job_log(job, "txt 完成：%d/%d 章 -> %s" % (done, total, out_path))
    return out_path


# ---------------- API ----------------
def _book_info(b):
    return {"id": b["id"], "title": b.get("title") or "", "author": b.get("author") or "",
            "site": b.get("site") or "", "url": b.get("url"),
            "chapter_count": b.get("chapter_count", len(b.get("chapters") or [])),
            "resolved": bool(b.get("resolved")), "reader_ok": b.get("reader_ok"),
            "error": b.get("error", "")}


@app.get("/api/config")
def api_config():
    return {"base": GW_BASE, "reader_dir": config["reader_dir"],
            "xpaths": gw.GW_XPATHS, "layer_stats": fetcher.layer_stats}


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1)):
    results, errors = aggregate_search(fetcher, q)
    results = _merge_shelf(q, results)
    out = []
    for r in results:
        if r.get("src") != "shelf":
            bid = store.register(r["url"], title=r["title"], site=r["site"])
            r = {"id": bid, **r}
        out.append(r)
    return {"results": out, "errors": errors}


@app.post("/api/resolve")
async def api_resolve(req: Request):
    body = await req.json()
    url = body.get("url", "").strip()
    want_txt = bool(body.get("txt"))
    title = body.get("title") or None
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    bid = store.register(url, title=title or "")
    job = _new_job("resolve")
    t = threading.Thread(target=_run_resolve_job, args=(job, bid, url, want_txt), daemon=True)
    t.start()
    return {"job_id": job["id"], "book_id": bid}


@app.get("/api/job/{jid}")
def api_job(jid: str):
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return job


@app.get("/api/books")
def api_books():
    return {"books": [_book_info(b) for b in store.all(shelf_only=True)]}


@app.post("/api/delete/{bid}")
def api_delete(bid: str):
    if not store.delete(bid):
        return JSONResponse({"error": "no such book"}, status_code=404)
    return {"ok": True}


@app.post("/api/cache/{bid}")
def api_clear_cache(bid: str):
    """清空本书章节缓存（书架保留，重新阅读时重抓）。"""
    if store.get(bid) is None:
        return JSONResponse({"error": "no such book"}, status_code=404)
    store.clear_cache(bid)
    return {"ok": True}


@app.post("/api/txt/{bid}")
def api_txt(bid: str):
    b = store.get(bid)
    if b is None:
        return JSONResponse({"error": "no such book"}, status_code=404)
    job = _new_job("txt")

    def _run():
        try:
            b2, err = store.ensure_resolved(bid, fetcher)
            if err:
                job["status"] = "failed"; job["result"] = {"error": err}; return
            path = _export_txt(b2, job)
            job["result"] = {"txt_file": path}
            job["status"] = "done"; job["progress"] = 100
        except Exception as e:
            job["status"] = "failed"; job["result"] = {"error": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job["id"]}


@app.post("/api/refresh/{bid}")
def api_refresh(bid: str):
    b = store.get(bid)
    if b is None:
        return JSONResponse({"error": "no such book"}, status_code=404)
    job = _new_job("refresh")

    def _run():
        b2, err = store.ensure_resolved(bid, fetcher, force=True)
        if err:
            job["status"] = "failed"; job["result"] = {"error": err}; return
        job["result"] = _book_info(b2)
        job["status"] = "done"; job["progress"] = 100

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job["id"]}


def _quick_chapter(b, ch, fetcher):
    """体检用快速抓单章：单次请求 + xpath + 提取（无重试无分页跟随）。"""
    from autobs import parse_doc, extract_content_text
    r = fetcher.get(ch["url"], referer=b.get("list_url"), quick=True)
    if r is None:
        return None
    doc = parse_doc(r)
    if doc is None:
        return None
    got = doc.xpath((b.get("src") or {}).get("content_xpath", ""))
    if len(got) != 1:
        return None
    return extract_content_text(got[0])


@app.post("/api/healthcheck")
def api_healthcheck():
    """一键体检：书架每本书 -> 站点探活 + 目录抽查 + 首/中/末章正文质量断言。
    报告同时落盘 data/health.json（书架页显示最近体检结果）。"""
    job = _new_job("healthcheck")

    def _run():
        import sys as _sys
        _sys.path.insert(0, BASE_DIR)
        from tests.quality import check_chapter_text
        books = store.all(shelf_only=True)
        report = []
        for i, b in enumerate(books):
            entry = {"id": b["id"], "title": (b.get("title") or "")[:30],
                     "site": b.get("site") or "", "alive": False,
                     "catalog": 0, "chapters": [], "ok": False}
            # 站点探活（快速模式：单次 8s）
            r = fetcher.get(b.get("list_url") or b.get("url"), quick=True)
            entry["alive"] = r is not None and len(r.content) > 1000
            # 目录（用已有解析结果，不强制重解析）
            b2, err = store.get_catalog(b["id"], fetcher)
            entry["catalog"] = len(b2.get("chapters") or []) if b2 else 0
            # 抽查：首章 quick 抓取断言；中/末章只读已有缓存
            if b2 and entry["catalog"]:
                import hashlib as _h, os as _os
                n = entry["catalog"]
                cache_dir = _os.path.join(BASE_DIR, "data", "cache", b["id"])
                for idx in sorted({0, n // 2, n - 1}):
                    ch = b2["chapters"][idx]
                    text = None
                    cpath = _os.path.join(cache_dir, _h.md5(ch["url"].encode()).hexdigest()[:16] + ".txt")
                    if _os.path.exists(cpath):
                        try:
                            text = open(cpath, encoding="utf-8").read()
                        except Exception:
                            text = None
                    checked = True
                    if text is None and idx == 0:
                        text = _quick_chapter(b2, ch, fetcher)
                    if text is None:
                        # 中/末章未读无缓存属正常；首章抓不到才是问题
                        checked = idx == 0
                        item = {"idx": idx + 1, "title": ch["title"][:20],
                                "len": 0,
                                "problems": ["首章快抓失败"] if idx == 0 else []}
                    else:
                        item = {"idx": idx + 1, "title": ch["title"][:20],
                                "len": len(text),
                                "problems": check_chapter_text(text) if checked else []}
                    entry["chapters"].append(item)
            # 可用性判定：目录在 + 首章质量过关（站点暂不可达只降级为警告，
            # 已缓存章节仍可读）
            entry["ok"] = bool(entry["catalog"]) and all(
                not c["problems"] for c in entry["chapters"])
            report.append(entry)
            job["progress"] = int(100 * (i + 1) / max(1, len(books)))
            _job_log(job, "体检 %d/%d：%s %s" % (i + 1, len(books), entry["title"],
                                                 "✓" if entry["ok"] else "发现问题"))
        # 落盘 + 完成报告
        try:
            import json as _json
            _json.dump({"time": time.time(), "books": report},
                       open(os.path.join(BASE_DIR, "data", "health.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass
        job["result"] = {"books": report,
                         "n_ok": sum(1 for e in report if e["ok"]),
                         "n_total": len(report)}
        job["status"] = "done"; job["progress"] = 100

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job["id"]}


@app.get("/api/health")
def api_health():
    """最近一次体检报告（无则空）。"""
    p = os.path.join(BASE_DIR, "data", "health.json")
    if not os.path.exists(p):
        return {"books": []}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {"books": []}


# ---------------- 网关（Reader 书源指向这里） ----------------
def _merge_shelf(keyword, results):
    """把书架上书名匹配的书置顶并入搜索结果——解析过的书在 Reader
    搜索框必须始终可搜到（网关 id 直达，无需重新解析）。"""
    import re as _re
    key = _re.sub(r"\s+", "", keyword)
    merged = []
    seen = set()
    for b in store.all(shelf_only=True):
        if key and key in _re.sub(r"\s+", "", b.get("title") or ""):
            merged.append({"id": b["id"], "title": b["title"], "url": b["url"],
                           "site": b.get("site") or "书架", "src": "shelf",
                           "author": b.get("author") or ""})
            seen.add(b["url"].rstrip("/"))
    for r in results:
        if (r.get("url") or "").rstrip("/") in seen:
            continue  # 书架已有同 URL，避免重复条目
        merged.append(r)
        seen.add((r.get("url") or "").rstrip("/"))
    return merged


@app.get("/g/search", response_class=HTMLResponse)
def g_search(q: str = Query(...)):
    results, _ = aggregate_search(fetcher, q)
    results = _merge_shelf(q, results)
    items = []
    for r in results:
        if r.get("src") == "shelf":
            bid = r["id"]
        else:
            bid = store.register(r["url"], title=r["title"], site=r["site"])
        items.append({"id": bid, "title": r["title"],
                      "author": r.get("author") or "", "site": r["site"]})
    return gw.render_search_page(items, GW_BASE)


@app.get("/g/book/{bid}", response_class=HTMLResponse)
def g_book(bid: str, refresh: int = 0):
    b, err = (store.ensure_resolved(bid, fetcher, force=True) if refresh
              else store.get_catalog(bid, fetcher))
    if err:
        return HTMLResponse("<html><body><p>解析失败: %s</p></body></html>" % err,
                            status_code=200)
    return gw.render_catalog_page(b, GW_BASE)


@app.get("/g/chapter/{bid}/{n}", response_class=HTMLResponse)
def g_chapter(bid: str, n: int):
    b = store.get(bid)
    title = b["chapters"][n - 1]["title"] if b and b.get("chapters") and 1 <= n <= len(b["chapters"]) else ""
    text = store.fetch_chapter(bid, n, fetcher)
    if text is None:
        return HTMLResponse("<html><body><div id='bh-content'>本章抓取失败，"
                            "请稍后重试或刷新目录。</div></body></html>")
    return gw.render_chapter_page(title, text)


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8").read()


if __name__ == "__main__":
    import uvicorn
    print("bookhub 启动: %s  (浏览器打开开始找书)" % GW_BASE)
    print("网关书源文件: %s （导入 Reader 一次，之后 Reader 搜索框直搜所有站）"
          % os.path.join(BASE_DIR, "bs_gateway.json"))
    uvicorn.run(app, host=config["host"], port=config["port"], log_level="warning")
