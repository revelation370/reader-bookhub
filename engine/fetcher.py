# -*- coding: utf-8 -*-
"""分层抓取器：L1 requests → L2 cloudscraper → L3 真浏览器(DrissionPage)。

对外接口与 autobs.Fetcher 兼容：get(url, referer=)/post(url, data, referer=)
返回 requests.Response 兼容对象（.status_code/.content/.url/.text），失败返回 None。
"""
import re
import threading
import time

import os
import requests
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "autobs"))
from autobs import UA, is_waf_page

try:
    import cloudscraper
except ImportError:
    cloudscraper = None


class BrowserResponse:
    """把 DrissionPage 页面包装成 requests.Response 兼容对象。
    text 是浏览器已正确解码的字符串；parse_doc 对 browser 标记的响应
    直接用 text 构建，避免 lxml 对无 meta 声明的 bytes 猜错编码。"""

    def __init__(self, url, html):
        self.status_code = 200
        self.url = url
        self.content = html.encode("utf-8", errors="ignore")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.text = html
        self.browser = True


class BrowserLayer:
    """L3：真浏览器（Chrome 优先，Edge 兜底），懒启动、进程内单例。"""

    def __init__(self, headless=True):
        self.headless = headless
        self._page = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._page is not None:
            return self._page
        from DrissionPage import ChromiumPage, ChromiumOptions
        import shutil
        opts = ChromiumOptions()
        browser = None
        for name, path in (
            ("chrome", shutil.which("chrome") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            ("msedge", shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ):
            if path and shutil.which(path) or (path and __import__("os").path.exists(path)):
                opts.set_browser_path(path)
                opts.set_paths(browser_path=path)
                browser = name
                break
        if browser is None:
            raise RuntimeError("未找到 Chrome/Edge 浏览器")
        if self.headless:
            opts.headless()
        opts.set_argument("--disable-blink-features=AutomationControlled")
        opts.set_argument("--no-first-run")
        opts.set_argument("--no-default-browser-check")
        self._page = ChromiumPage(opts)
        return self._page

    def get(self, url, timeout=25):
        with self._lock:
            page = self._ensure()
            page.get(url, timeout=timeout)
            # 等 JS 挑战跳转完成（CF 会有 5 秒等待页）
            deadline = time.time() + 10
            while time.time() < deadline:
                html = page.html or ""
                if html and not is_waf_page(html.encode("utf-8", errors="ignore")):
                    break
                time.sleep(1)
            return BrowserResponse(page.url or url, page.html or "")

    def close(self):
        with self._lock:
            if self._page is not None:
                try:
                    self._page.quit()
                except Exception:
                    pass
                self._page = None


class LayeredFetcher:
    """get/post 自动分层：普通请求失败或命中 WAF 时逐级升级。"""

    def __init__(self, debug=False, headless=True):
        self.s = requests.Session()
        self.s.trust_env = False  # 书站均为国内直连站，绕过系统代理（代理常导致 RST/超时）
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        self._scraper = None
        self._browser = None
        self._headless = headless
        self.debug = debug
        self.last = 0.0
        self.layer_stats = {"L1": 0, "L2": 0, "L3": 0}
        # 每站熔断：{host: [连续失败数, 冷却截止时间戳]}
        # 连续 3 次全层失败 → 冷却 90 秒（期间直接快速失败，不重试），
        # 避免站方限流时每个请求都走完整三层重试把调用方拖死
        self._host_state = {}
        self._host_lock = threading.Lock()

    def _host_of(self, url):
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc.lower()
        except Exception:
            return ""

    def _host_blocked(self, url):
        host = self._host_of(url)
        with self._host_lock:
            st = self._host_state.get(host)
            return bool(st and time.time() < st[1])

    def _host_fail(self, url):
        host = self._host_of(url)
        with self._host_lock:
            st = self._host_state.setdefault(host, [0, 0])
            st[0] += 1
            if st[0] >= 3:
                st[1] = time.time() + 90
                st[0] = 0
                if self.debug:
                    print("[fuse] %s 冷却 90s" % host)

    def _host_ok(self, url):
        host = self._host_of(url)
        with self._host_lock:
            if host in self._host_state:
                self._host_state[host] = [0, 0]

    # ---- 对外接口（autobs 兼容） ----
    def get(self, url, **kw):
        return self._req("GET", url, **kw)

    def post(self, url, data=None, **kw):
        return self._req("POST", url, data=data, **kw)

    def _throttle(self):
        dt = time.time() - self.last
        if dt < 0.6:
            time.sleep(0.6 - dt)
        self.last = time.time()

    def _ok(self, r):
        return r is not None and r.status_code == 200 and not is_waf_page(r.content)

    # "JS 设 cookie 后刷新"验证墙（ge_js_validator 等）：提取 cookie 重试即过，免开浏览器
    _JS_WALL = re.compile(
        r'document\.cookie\s*=\s*["\']([^"\']+)["\'][^<]{0,200}?location\.(?:reload|href)',
        re.S)

    def _try_js_wall(self, url, r):
        """若是 JS cookie 验证墙：把墙要求的 cookie 写入会话，返回 True（调用方重试）。"""
        if len(r.content) >= 2048:
            return False
        try:
            m = self._JS_WALL.search(r.content.decode("utf-8", errors="replace"))
        except Exception:
            return False
        if not m:
            return False
        kv = m.group(1).split(";")[0]
        if "=" not in kv:
            return False
        from urllib.parse import urlparse
        name, _, val = kv.partition("=")
        try:
            self.s.cookies.set(name.strip(), val.strip(),
                               domain=urlparse(url).netloc, path="/")
            if self._scraper is not None:
                self._scraper.cookies.set(name.strip(), val.strip(),
                                          domain=urlparse(url).netloc, path="/")
        except Exception:
            return False
        return True

    def _req(self, method, url, data=None, referer=None, force_layer=None,
             quick=False):
        # 熔断检查：该站冷却中直接快速失败
        if self._host_blocked(url):
            return None
        if quick:
            return self._req_quick(method, url, data, referer)
        self._throttle()
        headers = {}
        if referer:
            headers["Referer"] = referer
        if isinstance(data, (bytes, str)):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        self._last_hard_fail = False  # 业务失败(404等)不算站点故障，不计熔断
        result = self._req_layers(method, url, data, headers, referer, force_layer)
        if result is not None:
            self._host_ok(url)
        elif self._last_hard_fail:
            self._host_fail(url)
        return result

    def _req_quick(self, method, url, data, referer):
        """快速模式：单次请求 8s 超时，失败即 None（体检等场景用，
        不做重试/分层升级，保证批量探测的时效）。"""
        headers = {}
        if referer:
            headers["Referer"] = referer
        if isinstance(data, (bytes, str)):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            r = self.s.get(url, headers=headers, timeout=8) if method == "GET"                 else self.s.post(url, data=data, headers=headers, timeout=8)
            if self._ok(r):
                self._host_ok(url)
                return r
        except requests.RequestException:
            pass
        return None

    def _req_layers(self, method, url, data, headers, referer, force_layer):

        # L1 requests（这些书站网络不稳常见，退避重试 3 次）
        if force_layer in (None, "L1"):
            for attempt in range(3):
                try:
                    r = self.s.get(url, headers=headers, timeout=15) if method == "GET" \
                        else self.s.post(url, data=data, headers=headers, timeout=15)
                    if self._ok(r):
                        self.layer_stats["L1"] += 1
                        return r
                    if r.status_code == 200 and self._try_js_wall(url, r):
                        continue  # JS cookie 墙：已写入过墙 cookie，立即重试
                    if r.status_code not in (403, 503, 429) and not is_waf_page(r.content):
                        # 404 等业务错误：不升级，直接失败（不算站点故障）
                        if r.status_code != 200:
                            if r.status_code in (404, 500):
                                self._last_hard_fail = False
                                return None
                            return r
                    break
                except requests.RequestException:
                    if attempt < 2:
                        time.sleep(2 + attempt * 3)
                        continue

        # L2 cloudscraper（GET/POST 都支持）
        if force_layer in (None, "L2") and cloudscraper is not None:
            for attempt in range(2):
                try:
                    if self._scraper is None:
                        self._scraper = cloudscraper.create_scraper(
                            browser={"custom": UA[:120]})
                    r = self._scraper.get(url, headers=headers, timeout=20) if method == "GET" \
                        else self._scraper.post(url, data=data, headers=headers, timeout=20)
                    if self._ok(r):
                        self.layer_stats["L2"] += 1
                        return r
                except Exception:
                    if attempt == 0:
                        time.sleep(2)
                        continue

        # 走到这里说明 L1/L2 都没成 —— 后续再失败属网络/防护层故障
        self._last_hard_fail = True

        # L3 真浏览器（只支持 GET；POST 站点极少被拦到需要 L3）
        if force_layer in (None, "L3") and method == "GET":
            try:
                if self._browser is None:
                    self._browser = BrowserLayer(headless=self._headless)
                r = self._browser.get(url)
                if self._ok(r):
                    self.layer_stats["L3"] += 1
                    return r
            except Exception as e:
                if self.debug:
                    print("[L3] browser failed: %s" % e)
        return None
