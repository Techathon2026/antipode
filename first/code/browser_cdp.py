#!/usr/bin/env python3
"""极简 CDP 客户端：借一个真实浏览器，把"登录 → 取登录态 → 读渲染后的页面"跑通。

为什么必须有这一层
------------------
抖音、微博的列表是前端渲染的，纯 HTTP 抓 HTML 只能拿到一个空壳；
它们真正取数据的内部接口还要签名参数（x-bogus / a_bogus），
跟着平台版本变，逆向出来两天就废。

用 CDP 去看"用户已经看到的那个页面"，两个问题一起解决：

1. **登录**：让用户在真实浏览器窗口里自己登（含验证码、短信、2FA）。
   密码不经过我们的代码，也不落盘 —— 这是唯一既合规又稳定的做法。
2. **取数**：拿到的是渲染后的 DOM，SPA 也能抽链接和标题。

依赖
----
websockets（**可选**，只在用到浏览器时导入）。缺了会给出明确提示，
其它模块（抓正文 / 判立场 / 检索）完全不受影响。

用法
----
    from browser_cdp import Browser

    b = Browser()                 # 有头，用户看得见
    b.start()
    b.goto("https://www.douyin.com/user/xxx")
    input("登录完成后回车...")
    b.scroll(8)
    items = b.eval_js(EXTRACT_JS)
    cookies = b.cookies()         # 含 HttpOnly，document.cookie 拿不到的那些
    b.close()
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

# Edge / Chrome 的常见安装位置（Windows 优先，其次 macOS / Linux）
_BROWSER_CANDIDATES = (
    os.environ.get("ANTIPODE_BROWSER", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

_DEVNULL = subprocess.DEVNULL


def find_browser() -> str:
    """找一个可用的 Chromium 内核浏览器。"""
    for p in _BROWSER_CANDIDATES:
        if p and os.path.exists(p):
            return p
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError(
        "找不到 Edge / Chrome。装一个，或用 ANTIPODE_BROWSER=<可执行文件路径> 指定。"
    )


def _free_port(start: int = 9222, span: int = 40) -> int:
    for port in range(start, start + span):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("9222–9261 都被占用了")


def has_websockets() -> bool:
    try:
        import websockets  # noqa: F401
        return True
    except Exception:                                          # noqa: BLE001
        return False


class Browser:
    """一次会话：启动浏览器 → 操作页面 → 退出。

    刻意做成"用完即弃"：**独立的临时 profile**，不碰用户日常浏览器的数据。
    代价是每次要在窗口里登录一次；换来的是不会误读、误传用户其它账号的信息。
    `profile_dir` 可以指定固定目录，登录态就能跨次复用。
    """

    def __init__(
        self,
        profile_dir: str = "",
        headless: bool = False,
        port: int = 0,
        exe: str = "",
        keep_open: bool = False,
    ) -> None:
        self.exe = exe or find_browser()
        self.port = port or _free_port()
        self.profile = profile_dir or tempfile.mkdtemp(prefix="antipode-profile-")
        self.headless = headless
        self.keep_open = keep_open
        self.proc: subprocess.Popen | None = None
        self.target: dict = {}
        self.version: dict = {}

    # -- 生命周期 ---------------------------------------------------------
    def start(self, timeout: float = 30.0) -> "Browser":
        args = [
            self.exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-features=Translate,MediaRouter",
            "--window-size=1280,900",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        self.proc = subprocess.Popen(args, stdout=_DEVNULL, stderr=_DEVNULL)
        self._wait_ready(timeout)
        return self

    def _wait_ready(self, timeout: float) -> None:
        t0 = time.time()
        last = ""
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=1
                ) as resp:
                    self.version = json.loads(resp.read().decode("utf-8", "replace"))
                    return
            except Exception as e:                             # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                time.sleep(0.25)
        raise RuntimeError(f"浏览器起来了但调试端口没响应（{last}）")

    def close(self) -> None:
        """关窗口。keep_open 时不关，方便用户接着看。"""
        if self.keep_open:
            return
        try:
            self._http("/json/close/" + self.target.get("id", ""), method="GET")
        except Exception:                                      # noqa: BLE001
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:                                  # noqa: BLE001
                self.proc.kill()

    # -- HTTP 端点 -------------------------------------------------------
    def _http(self, path: str, method: str = "GET", timeout: float = 10.0) -> dict | list:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}

    def new_page(self, url: str = "about:blank") -> dict:
        """开新标签页并记住它。

        注意：`/json/new?<url>` 这个参数在不同 Chromium 版本上行为不一致
        （有的直接导航，有的忽略），所以这里**只建空白页**，导航统一走
        `Page.navigate` —— 一条路径，不赌版本。
        """
        try:
            self.target = self._http("/json/new?about:blank", method="PUT") or {}
        except Exception:
            # 老版本 Chromium 只认 GET
            self.target = self._http("/json/new?about:blank", method="GET") or {}
        if not self.target.get("webSocketDebuggerUrl"):
            raise RuntimeError("新标签页没拿到调试地址，浏览器版本可能不兼容")
        if url and url != "about:blank":
            self.goto(url)
        return self.target

    # -- 与页面通信 ------------------------------------------------------
    def _send(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        if not has_websockets():
            raise RuntimeError(
                "这个功能需要 websockets：pip install websockets\n"
                "（只在「用浏览器登录采集」时才需要；抓正文、判立场、反推荐都不需要它）"
            )
        import asyncio

        return asyncio.run(self._send_async(method, params or {}, timeout))

    async def _send_async(self, method: str, params: dict, timeout: float) -> dict:
        import asyncio

        from websockets.asyncio.client import connect

        ws_url = self.target["webSocketDebuggerUrl"]
        async with connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=15) as ws:
            await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if msg.get("id") == 1:
                    return msg

    def eval_js(self, expr: str, timeout: float = 30.0):
        """在页面里跑一段 JS，返回它的值（按值传回）。"""
        resp = self._send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if "error" in resp:
            raise RuntimeError(f"CDP 报错：{resp['error'].get('message')}")
        res = resp.get("result", {})
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"]
            raise RuntimeError(f"页面 JS 抛错：{d.get('text')} {json.dumps(d.get('exception', {}), ensure_ascii=False)[:200]}")
        return res.get("result", {}).get("value")

    # -- 常用动作 ---------------------------------------------------------
    def goto(self, url: str, wait: float = 0.0, timeout: float = 30.0) -> str:
        """导航并**确实**等到新文档加载完成。返回最终 URL（会被重定向）。

        这里不能用 readyState 直接判 —— 刚发完 navigate，读到的还是**旧文档**的
        readyState（本来就是 complete），会立刻"以为加载完了"，然后在新文档的
        半成品 DOM 上抽链接，抽到的是一两个节点。实测踩过：HN 只抽到 1 个链接。

        所以先在新文档的 window 上留个标记，导航后标记消失 = 新文档已接管，
        再等 readyState complete。跨域导航必触发，同源导航由超时兜底。
        """
        if not self.target:
            self.new_page("about:blank")
        try:
            self.eval_js("window.__antipode_nav = 1", timeout=5)
        except Exception:                                      # noqa: BLE001
            pass

        self._send("Page.navigate", {"url": url})

        # 跨域：标记消失即新文档已接管，再给 0.6s 让首屏 DOM 稳定。
        # 同源：window 不重置，标记不会消失 —— 最多等 2.5s 就往下走，
        # 剩余靠 scroll 的 pause 兜底（宁可早走，也不要卡满 30s）。
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.2)
            try:
                mark = self.eval_js("window.__antipode_nav", timeout=4)
            except Exception:                                  # noqa: BLE001
                continue
            if mark is None:
                time.sleep(0.6)
                break
            if time.time() - t0 > 2.5:
                break
        # SPA 的 readyState 早就 complete，但首屏数据还在路上
        if wait:
            time.sleep(wait)
        try:
            return self.eval_js("location.href") or url
        except Exception:                                      # noqa: BLE001
            return url

    def _wait_loaded(self, timeout: float) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.eval_js("document.readyState", timeout=5) == "complete":
                    return
            except Exception:                                  # noqa: BLE001
                pass
            time.sleep(0.3)
        # 超时不报错：SPA 常有长连接，readyState 迟迟不 complete 也能用

    def scroll(self, times: int = 6, pause: float = 1.2) -> int:
        """往下滚，触发懒加载。返回滚完的页面高度。

        容错到"滚不动也继续"：SPA 换页的瞬间 document.body 可能是 null，
        实测微博跳转时就会抛 TypeError。滚不动不是错误，不该中断采集。
        """
        js = """(() => {
          try {
            const el = document.scrollingElement || document.documentElement || document.body;
            if (!el) return 0;
            window.scrollTo(0, el.scrollHeight);
            return el.scrollHeight || 0;
          } catch (e) { return 0; }
        })()"""
        height = 0
        for _ in range(max(0, times)):
            try:
                height = int(self.eval_js(js) or 0)
            except Exception:                                  # noqa: BLE001
                pass
            time.sleep(pause)
        return height

    def cookies(self) -> dict[str, list[dict]]:
        """取全部 Cookie（含 HttpOnly —— 这正是 document.cookie 拿不到的部分）。"""
        self._send("Network.enable")
        resp = self._send("Network.getAllCookies", timeout=20)
        out: dict[str, list[dict]] = {}
        for c in (resp.get("result", {}) or {}).get("cookies", []) or []:
            out.setdefault(c.get("domain", ""), []).append(c)
        return out

    def cookie_header(self, host_hint: str = "") -> str:
        """拼成可直接放进请求头的 `k=v; k2=v2`，可只取与某个域名相关的。"""
        allck = self.cookies()
        picked: list[dict] = []
        for domain, lst in allck.items():
            if host_hint and host_hint not in domain and domain.lstrip(".") not in host_hint:
                continue
            picked.extend(lst)
        # 大域在前，避免子域覆盖主域同名 Cookie
        picked.sort(key=lambda c: len(c.get("domain", "")), reverse=True)
        seen: dict[str, str] = {}
        for c in picked:
            seen[c.get("name", "")] = c.get("value", "")
        return "; ".join(f"{k}={v}" for k, v in seen.items() if k)

    # -- 调试用 -----------------------------------------------------------
    def info(self) -> str:
        return f"{os.path.basename(self.exe)} :{self.port}  profile={self.profile}"


if __name__ == "__main__":                                    # pragma: no cover
    b = Browser(headless=False)
    b.start()
    b.new_page("https://example.com")
    print(b.info())
    print("标题：", b.eval_js("document.title"))
    print("Cookie 域名数：", len(b.cookies()))
    b.close()
    print("ok")
    sys.exit(0)
