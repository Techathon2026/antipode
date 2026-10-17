#!/usr/bin/env python3
"""模块 ⓪：从链接取回正文 —— 因为队友只给 URL。

【为什么需要这一块】
原本的设计假设是：队友的库条目里带 `full_text`。
实际上他只给**原文章的地址**。而反推荐要做的第一件事是"这条内容站哪一边"，
这件事读摘要读不出来（省流版会把作者态度抹平），必须读正文。

所以补一层：URL → 正文。

【它做不到什么（必须先说清楚）】
不是所有站点都能抓。2026-10-17 实测：

    ✅ CSDN       200，正文可取
    ✅ GitHub     200，可取（噪声多，靠分块过滤）
    ✅ 少数派      200，可取
    ❌ V2EX       SSL EOF / 超时（本机到 v2ex.com 直连不通）
    ❌ 知乎        403（反爬）
    ❌ 公众号      JS 渲染，静态 HTML 里没有正文

抓不到时**绝不编正文**：返回 ok=False + 明确错误原因，
条目落到 `fetch_failed` 归宿，并在报告里提示"补一行 full_text 就能跑"。
这条界线是整个系统"不幻觉"的前提 —— 宁可少一条反信息，也不能拿猜测去判立场。

【三层策略，从便宜到贵】
    ① 站点适配器   已知结构的站点直接定位正文容器（最准）
    ② 主容器启发   按 article / .markdown-body / #content_views 等常见容器取
    ③ 分块打分     通用兜底：按块切、链接密度高的块（导航/推荐）丢掉

只依赖标准库 —— 现场换机器、断网重装依赖都不会挂。
"""

from __future__ import annotations

import gzip
import html as _html
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 抓正文的最小长度。低于这个数说明"抓到的不是文章"（多半是登录墙、验证页、空壳）。
MIN_ARTICLE_CHARS = 80

# 站点 → 正文容器。这些站点的结构稳定，直接定位比通用打分准得多。
SITE_RULES: dict[str, dict] = {
    "csdn.net": {
        "container": r'<div[^>]+id=["\']content_views["\'][^>]*>',
        "stop": r'<div[^>]+(?:id=["\']treeSkill|class=["\'][^"\']*comment-box)'
                r'|<div[^>]+class=["\'][^"\']*recommend|<footer',
        "noise": r"^(\s*目录|\s*登录|\s*关注|\s*点赞|\s*收藏|\s*评论|\s*分享|\s*举报|\s*上一篇|\s*下一篇)",
        "name": "csdn",
    },
    "github.com": {
        "container": r'<div[^>]+class=["\'][^"\']*markdown-body[^"\']*["\'][^>]*>',
        "stop": r'<div[^>]+(?:class=["\'][^"\']*timeline-comment-actions|id=["\']partial-timeline)'
                r'|<footer',
        "noise": r"^(Skip to content|Sign in|Navigation Menu|Search or jump)",
        "name": "github",
    },
    "sspai.com": {
        "container": r'<div[^>]+class=["\'][^"\']*(article-body|content)[^"\']*["\'][^>]*>',
        "stop": r'<footer|<div[^>]+class=["\'][^"\']*(comment|related|recommend)',
        "noise": r"",
        "name": "sspai",
    },
    "mp.weixin.qq.com": {
        "container": r'<div[^>]+class=["\'][^"\']*rich_media_content[^"\']*["\'][^>]*>',
        "stop": r'<div[^>]+class=["\'][^"\']*rich_media_tool|<script',
        "noise": r"",
        "name": "weixin",
    },
}

# 通用容器候选（按优先级）。命中即用它内部的文本，不再走分块打分。
GENERIC_CONTAINERS = [
    r'<article[^>]*>',
    r'<div[^>]+id=["\'](?:content|article|post|main-content)["\'][^>]*>',
    r'<div[^>]+class=["\'][^"\']*markdown-body[^"\']*["\'][^>]*>',
    r'<div[^>]+class=["\'][^"\']*(?:article-content|post-content|entry-content|rich_media_content|article__content)["\'][^>]*>',
    r'<main[^>]*>',
]

# 通用停止点：页脚 / 侧栏 / 评论 / 相关推荐
GENERIC_STOP = (r'<footer|<aside|<div[^>]+(?:class|id)=["\'][^"\']*'
                r'(?:comment|related|recommend|footer|sidebar)')

# 标题里要剥掉的站点尾巴
TITLE_TAIL = re.compile(
    r"\s*[-_|–—]\s*(CSDN博客|CSDN|掘金|知乎|少数派|V2EX|SegmentFault|博客园|简书|"
    r"GitHub|掘金社区|InfoQ|博客).*$"
)


@dataclass
class FetchResult:
    ok: bool
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    method: str = ""          # site / container / blocks / ""
    status: int = 0
    chars: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "url": self.url, "final_url": self.final_url,
            "title": self.title, "chars": self.chars, "method": self.method,
            "status": self.status, "error": self.error,
        }


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
# 登录态：用户在自己浏览器里登录之后，把 Cookie 导出来给我用。
#
# 【为什么是"用户导 Cookie"而不是"我登录他的账号"】
#   ① 登录需要手机验证码 / 扫码 —— 我拿不到，也不该拿
#   ② 账号凭据一旦进了项目目录，就有被提交进仓库的风险（这是比赛仓库，会公开）
#   ③ 平台条款禁止把账号交给第三方程序
# 所以正确的边界是：**用户在自己的浏览器里完成登录，只把该站点的会话 Cookie 交出来**，
# 我只用它去读用户已经有权看到的那一篇文章。Cookie 只放本地、不进仓库、不打印。
#
# 用法：
#   export ANTIPODE_COOKIES=/path/to/cookies.json     （推荐，文件不进仓库）
# 或者 .env 里写一行 ANTIPODE_COOKIES=...
#
# cookies.json 两种写法都认：
#   {"zhihu.com": "z_c0=xxx; d_c0=yyy",
#    "mp.weixin.qq.com": "..."}
#   {"cookie": "k=v; k2=v2"}          # 全局，不区分站点的懒人写法
COOKIE_ENV = "ANTIPODE_COOKIES"
_COOKIE_CACHE: dict | None = None


def _load_cookies() -> dict:
    global _COOKIE_CACHE
    if _COOKIE_CACHE is not None:
        return _COOKIE_CACHE
    path = os.environ.get(COOKIE_ENV, "").strip()
    data: dict = {}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data = {str(k).lower(): str(v) for k, v in raw.items() if str(v).strip()}
        except Exception as e:                                    # noqa: BLE001
            print(f"[!] Cookie 文件读不出来，忽略：{e}", file=sys.stderr)
    _COOKIE_CACHE = data
    return data


def cookie_for(url: str) -> str:
    """取该站点可用的 Cookie 字符串（找不到返回空串）。"""
    data = _load_cookies()
    if not data:
        return ""
    host = _site_of(url)
    # 精确 → 后缀匹配（zhihu.com 能覆盖 www.zhihu.com / zhuanlan.zhihu.com）
    for k, v in data.items():
        if k in ("cookie", "*") or k == host or host.endswith("." + k) or host == k:
            return v
    return ""


def _http(url: str, timeout: float) -> tuple[int, bytes, str, dict]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    ck = cookie_for(url)
    if ck:
        headers["Cookie"] = ck
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return resp.status, raw, resp.geturl(), dict(resp.headers)


def _decode(raw: bytes, headers: dict) -> str:
    ct = (headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w-]+)", ct)
    candidates = [m.group(1)] if m else []
    head = raw[:2048].decode("ascii", "ignore")
    m2 = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if m2:
        candidates.append(m2.group(1))
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        enc = (enc or "").lower()
        if enc in ("gb2312", "gbk"):
            enc = "gb18030"
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# HTML → 文本
# ---------------------------------------------------------------------------
_RE_SCRIPT = re.compile(r"(?is)<(script|style|noscript|svg|iframe|form|nav|footer|head)\b[^>]*>.*?</\1\s*>")
_RE_COMMENT = re.compile(r"(?is)<!--.*?-->")
_RE_BR = re.compile(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</li\s*>|</h[1-6]\s*>|</tr\s*>")
_RE_TAG = re.compile(r"(?s)<[^>]+>")
_RE_A = re.compile(r"(?is)<a\b[^>]*>(.*?)</a>")


def _clean_html(html: str) -> str:
    s = _RE_COMMENT.sub(" ", html)
    s = _RE_SCRIPT.sub(" ", s)
    return s


def _textify(html: str) -> str:
    s = _RE_BR.sub("\n", html)
    s = _RE_TAG.sub(" ", s)
    s = _html.unescape(s)
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"[ \t\u3000\xa0]+", " ", s)
    return s


def _lines(text: str, min_len: int = 12) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip(" ·|")
        if len(line) < min_len:
            continue
        out.append(line)
    return out


def _dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for l in lines:
        k = l[:60]
        if k in seen:
            continue
        seen.add(k)
        out.append(l)
    return out


def _find_block(html: str, open_tag_re: str, stop_re: str = "", max_chars: int = 40000) -> str:
    """从正文容器的开标签往后取一段，遇到"该停的地方"就停。

    ⚠️ 不用标签配对计数 —— 实测 CSDN 会把 `<div>` 塞在段落中间，
    配对法会在第一个 `</div>` 处把正文腰斩（855 字就断了，句子都没说完）。
    改用"从起点往后取，遇到评论/页脚/侧栏就停"，宁可多带一点，也不能把正文截断：
    多带的噪声后面有行级过滤兜着，截断的正文没法补救。
    """
    m = re.search(open_tag_re, html, re.I)
    if not m:
        return ""
    end = min(len(html), m.end() + max_chars)
    if stop_re:
        s = re.search(stop_re, html[m.end():end], re.I)
        if s and s.start() > 400:            # 太靠前的"停止点"多半是正文内部的，不认
            end = m.end() + s.start()
    return html[m.end():end]


def _blocks(html: str) -> list[str]:
    """通用兜底：按块切、按链接密度丢导航。"""
    s = _clean_html(html)
    parts = re.split(r"(?i)</(?:p|div|li|td|h[1-6]|section|article)\s*>", s)
    kept: list[str] = []
    for p in parts:
        links = "".join(_textify(a) for a in _RE_A.findall(p))
        text = _textify(p)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 40:
            continue
        link_ratio = (len(links) / max(1, len(text)))
        if link_ratio > 0.55:          # 导航 / 推荐位 / 目录
            continue
        if not re.search(r"[。！？.!?，,；;：:]", text):
            continue
        kept.append(text)
    return kept


def _title(html: str) -> str:
    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    if h1:
        t = re.sub(r"\s+", " ", _textify(h1.group(1))).strip()
        if 6 <= len(t) <= 120:
            return t
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if not m:
        return ""
    t = re.sub(r"\s+", " ", _textify(m.group(1))).strip()
    return TITLE_TAIL.sub("", t)[:120]


def _site_of(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def fetch_article(url: str, timeout: float = 20.0) -> FetchResult:
    """取一个链接的正文。失败时 ok=False，error 说明为什么。"""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return FetchResult(ok=False, url=url, error="不是 http(s) 链接")

    # 只允许公网地址（防止被一个内网链接骗去读本机文件/内网服务）
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return FetchResult(ok=False, url=url, error="拒绝访问本机地址")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return FetchResult(ok=False, url=url, error="拒绝访问内网地址")
    except ValueError:
        pass

    try:
        status, raw, final_url, headers = _http(url, timeout)
    except urllib.error.HTTPError as e:
        # 5xx 多半是 CDN / WAF 抖动。实测 CSDN 会**间歇性**返回 521：
        # 同一篇文章，几分钟前抓得到 1496 字，几分钟后连试两次都是 521。
        # 所以这里做退避重试（3 次，1.2s / 3s），而不是立刻宣判"抓不到"。
        # 4xx 不重试 —— 403/404 重试一百次也一样。
        if e.code < 500:
            return FetchResult(ok=False, url=url, status=e.code,
                               error=f"HTTP {e.code}" + ("（反爬/需登录）" if e.code in (401, 403) else ""))
        last, ok = e, False
        for wait in (1.0, 2.5, 5.0):
            time.sleep(wait)
            try:
                status, raw, final_url, headers = _http(url, timeout)
                ok = True
                break
            except urllib.error.HTTPError as e2:
                last = e2
            except Exception as e2:                              # noqa: BLE001
                return FetchResult(ok=False, url=url, status=e.code,
                                   error=f"HTTP {e.code}（重试失败：{type(e2).__name__}）")
        if not ok:
            return FetchResult(ok=False, url=url, status=last.code,
                               error=f"HTTP {last.code}（退避重试 3 次仍失败）")
    except urllib.error.URLError as e:
        reason = str(e.reason)
        hint = "（站点不可达/被墙）" if ("SSL" in reason or "timed out" in reason) else ""
        return FetchResult(ok=False, url=url, error=f"连不上{hint}：{reason[:60]}")
    except socket.timeout:
        return FetchResult(ok=False, url=url, error="超时")
    except Exception as e:                                        # noqa: BLE001
        return FetchResult(ok=False, url=url, error=f"{type(e).__name__}: {e}")

    page = _decode(raw, headers)
    if not page.strip():
        return FetchResult(ok=False, url=url, status=status, error="返回空白页")

    title = _title(page)
    clean = _clean_html(page)
    site = _site_of(final_url or url)

    # ① 站点适配
    rule = SITE_RULES.get(site)
    text, method = "", ""
    if rule:
        block = _find_block(clean, rule["container"], rule.get("stop", ""))
        if block:
            lines = _dedupe(_lines(_textify(block), 10))
            if rule.get("noise"):
                lines = [l for l in lines if not re.search(rule["noise"], l)]
            if sum(len(l) for l in lines) >= MIN_ARTICLE_CHARS:
                text, method = "\n".join(lines), "site"

    # ② 通用容器
    if not text:
        for pat in GENERIC_CONTAINERS:
            block = _find_block(clean, pat, GENERIC_STOP)
            if not block:
                continue
            lines = _dedupe(_lines(_textify(block), 10))
            if sum(len(l) for l in lines) >= MIN_ARTICLE_CHARS:
                text, method = "\n".join(lines), "container"
                break

    # ③ 分块打分
    if not text:
        blocks = _blocks(page)
        if blocks:
            text, method = "\n".join(_dedupe(blocks)), "blocks"

    if not text or len(text) < MIN_ARTICLE_CHARS:
        return FetchResult(
            ok=False, url=url, final_url=final_url, title=title, status=status,
            chars=len(text or ""),
            error="抓到页面但取不出正文（可能是登录墙/脚本渲染/正文过短）",
        )

    return FetchResult(ok=True, url=url, final_url=final_url, title=title,
                       text=text, method=method, status=status, chars=len(text))


def guess_source(url: str) -> str:
    """从链接推断来源，用于三档检索里的"第一档：回原条目所在的地方"。"""
    host = _site_of(url)
    table = (
        ("v2ex.com", "v2ex"), ("juejin.cn", "juejin"), ("csdn.net", "csdn"),
        ("github.com", "github"), ("news.ycombinator.com", "hn"),
        ("zhihu.com", "zhihu"), ("sspai.com", "sspai"),
        ("mp.weixin.qq.com", "weixin"), ("jianshu.com", "jianshu"),
        ("cnblogs.com", "cnblogs"),
    )
    for key, name in table:
        if host.endswith(key):
            return name
    return "web"


# ---------------------------------------------------------------------------
# 离线自检（用内置 HTML，不联网）
# ---------------------------------------------------------------------------
_FIXTURE_CSDN = """
<html><head><title>微服务的代价-CSDN博客</title></head><body>
<div class="nav"><a href="/">首页</a><a href="/x">推荐</a></div>
<div id="content_views"><h1>微服务的代价</h1>
<p>我们上了微服务两年，问题才慢慢显现：网络调用过多、运维成本增加、接口风格不一致。</p>
<p>所以我的结论是：小团队不要轻易拆，单体先把业务跑起来更重要。</p></div>
<div class="footer">登录 关注 点赞</div></body></html>
"""

_FIXTURE_GENERIC = """
<html><head><title>为什么我们放弃了 React</title></head><body>
<div class="sidebar"><a href="/a">文章一</a><a href="/b">文章二</a></div>
<article><p>选型时我们把两边都做了原型，Vue 的上手确实更快。</p>
<p>但我们最终判断生态和招人成本更关键，所以还是留在了 React，只是把组件库换掉了。</p></article>
</body></html>
"""


def selftest() -> int:
    print("=" * 72)
    print("模块 ⓪ 链接取正文 —— 离线自检（不联网）")
    print("=" * 72)
    ok = True

    # 站点适配路径
    title = _title(_FIXTURE_CSDN)
    block = _find_block(_clean_html(_FIXTURE_CSDN), SITE_RULES["csdn.net"]["container"],
                         SITE_RULES["csdn.net"]["stop"])
    lines = _dedupe(_lines(_textify(block), 10))
    text = "\n".join(lines)
    checks = [
        ("能从 title 剥掉站点尾巴", title == "微服务的代价", title),
        ("能定位 CSDN 正文容器", "网络调用过多" in text, text[:30]),
        ("不把导航算进正文", "首页" not in text and "登录" not in text, ""),
    ]

    # 通用容器路径
    block2 = _find_block(_clean_html(_FIXTURE_GENERIC), r"<article[^>]*>", GENERIC_STOP)
    text2 = "\n".join(_dedupe(_lines(_textify(block2), 10)))
    checks.append(("能用 <article> 兜底", "生态和招人成本" in text2, text2[:30]))

    # 推断来源
    checks.append(("能推断来源", guess_source("https://www.v2ex.com/t/1") == "v2ex", ""))
    checks.append(("未知域名回落 web", guess_source("https://blog.example.dev/p/1") == "web", ""))

    # 拒绝本地地址
    r = fetch_article("http://127.0.0.1:8000/x")
    checks.append(("拒绝访问本机地址", (not r.ok) and "本机" in r.error, r.error))

    for name, good, note in checks:
        print(f"{'✅' if good else '❌'} {name}" + (f"   （{note}）" if note else ""))
        ok = ok and good
    print("-" * 72)
    print("✅ 全部通过" if ok else "❌ 有检查未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
