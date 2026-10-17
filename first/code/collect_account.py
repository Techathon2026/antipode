#!/usr/bin/env python3
"""客户给一个网址 + 账号凭据 → 把他账号里的内容读出来，建一个库。

和 `build_vault.py` 的区别
------------------------
| | build_vault | collect_account |
|---|---|---|
| 输入 | 一堆文章链接 | **一个主页网址 + 登录凭据** |
| 难点 | 抓正文 | 先得登进去，才能看见列表 |
| 产出 | 同样格式的库 | 同样格式的库 |

两者产出完全一致，下游 `run_vault.py` 不用区分。

三种认证方式（按可靠性排序）
--------------------------
1. `--token`（GitHub）：最稳。去 https://github.com/settings/tokens 建一个只读 PAT。
2. `--cookie` / `--cookie-file`：浏览器里登录好，把 Cookie 复制出来。
3. `--browser`：**我们开一个浏览器窗口，你在里面登录**。
   含验证码、短信、二次验证都由你自己完成 —— 密码不经过这段代码。

关于"账号密码"
------------
这里**不做**"程序拿用户名密码去 POST 登录表单"：
  - 抖音 / 微博 / GitHub 现在登录几乎必过验证码或设备校验，脚本一登就红；
  - 把别人账号密码存在磁盘上，是最容易出事的一类漏洞。

实际做法是 `--browser --username ... --password ...`：
  程序把账号密码**填进输入框**（只在内存里，不落盘、不打印），
  然后停住，等你在窗口里点"登录"、过验证码。
效果一样，但密码不离开你的手，也不怕风控。

用法
----
    # GitHub：token 直接拉 star 过的项目
    python first/code/collect_account.py \\
        --url https://github.com/ciqiongovo --token ghp_xxx \\
        --vault-id gh-me --topic "我关注的技术"

    # 抖音：开窗口登录一次，自动滚动把作品列表拉完
    python first/code/collect_account.py \\
        --url https://www.douyin.com/user/MS4wLjABAAAA... --browser \\
        --vault-id dy-me --limit 20

    # 登录态存下来，后面抓正文也能用
    python first/code/collect_account.py --url ... --browser --save-cookies
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

from build_vault import (  # noqa: E402
    _item_id, _now, _norm_url, build_item, make_opts, write_vault,
)

CST_HINT = "+08:00"


# ---------------------------------------------------------------------------
# 1. 平台判定
# ---------------------------------------------------------------------------
PLATFORM_OF_HOST = (
    ("github.com", "github"),
    ("weibo.com", "weibo"),
    ("weibo.cn", "weibo"),
    ("douyin.com", "douyin"),
    ("bilibili.com", "bilibili"),
    ("zhihu.com", "zhihu"),
    ("xiaohongshu.com", "xiaohongshu"),
)

# 每个平台"一条内容"长什么样（CSS 选择器，选的是可点的那个 a）
CONTENT_SELECTOR = {
    # GitHub 主路径走 API，这个只在"用 --browser 看 GitHub 网页"时用
    "github": '[itemprop="name codeRepository"] a, a[href*="/issues/"]',
    "douyin": 'a[href*="/video/"]',
    "weibo": 'a[href*="/detail/"], a[href*="weibo.com/"][href*="/"][class]',
    "bilibili": 'a[href*="/video/BV"]',
    "zhihu": 'a[href*="/p/"], a[href*="/answer/"]',
    "xiaohongshu": 'a[href*="/explore/"]',
    "generic": "a[href]",
}

# 明显不是内容的路径
NAV_LIKE = re.compile(
    r"/(login|signup|register|logout|settings|about|help|privacy|terms|download"
    r"|explore|search|tag|tags|topic|topics|notification|message|hot|rank)(/|$|\?)"
    r"|/(index|default|home)\.html?$",
    re.I,
)


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for suffix, name in PLATFORM_OF_HOST:
        if host == suffix or host.endswith("." + suffix):
            return name
    return "generic"


# ---------------------------------------------------------------------------
# 2. GitHub：走官方 API，不碰浏览器
# ---------------------------------------------------------------------------
def _gh_get(path: str, token: str, timeout: float = 25.0):
    url = path if path.startswith("http") else "https://api.github.com" + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "antipode-collector",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) \
        if os.environ.get("ANTIPODE_NO_PROXY", "").strip().lower() in ("1", "true", "yes") \
        else urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def collect_github(url: str, token: str, scope: str, limit: int) -> list[dict]:
    """把"这个账号收藏/创建的东西"变成条目。

    repo 没有"立场"，所以这里**不调模型判立场**，直接把描述当省流。
    反推荐开关默认也建议关掉（代码项目找"反面观点"没有意义）。
    """
    m = re.search(r"github\.com/([^/?#]+)", url)
    if not m:
        raise RuntimeError(f"看不出 GitHub 用户名：{url}")
    owner = m.group(1)

    out: list[dict] = []
    if scope == "starred":
        # /users/{u}/starred 只能看公开 star；看自己的用 /user/starred
        path = "/user/starred?per_page=100" if not owner else f"/users/{owner}/starred?per_page=100"
        items = _gh_get(path, token)
        for r in items:
            out.append(_repo_item(r, owner, "starred"))
            if limit and len(out) >= limit:
                break
    elif scope == "repos":
        items = _gh_get(f"/users/{owner}/repos?per_page=100&sort=updated", token)
        for r in items:
            out.append(_repo_item(r, owner, "repos"))
            if limit and len(out) >= limit:
                break
    else:  # issues：自己开过/参与过的议题，这里面反而常有观点
        q = f"author:{owner}+is:issue"
        data = _gh_get(f"/search/issues?q={q}&per_page=50&sort=updated", token)
        for it in data.get("items", []):
            out.append({
                "url": it.get("html_url", ""),
                "title": it.get("title", ""),
                "text": (it.get("body") or "")[:2000],
                "kind": "issue",
                "antipode_eligible": False,     # 工作材料，没有"对立面"
                "summary": (it.get("title") or "")[:120],
                "summary_from": "title",
                "source": "github",
                "collected_by": "collect_account.py",
                "full_text": (it.get("body") or "")[:6000],
                "text_chars": len(it.get("body") or ""),
            })
            if limit and len(out) >= limit:
                break
    return [x for x in out if x.get("url")]


def _repo_item(r: dict, owner: str, how: str) -> dict:
    desc = (r.get("description") or "").strip()
    topics = r.get("topics") or []
    lang = r.get("language") or ""
    stars = r.get("stargazers_count") or 0
    # 给人看的一行：这是什么、用什么写的、多热
    bits = [desc] if desc else [r.get("name", "")]
    meta = " · ".join(x for x in (lang, f"{stars} star") if x)
    summary = f"{bits[0][:110]}" + (f"（{meta}）" if meta else "")
    text = "\n".join(x for x in [
        r.get("name", ""), desc, " ".join(topics), meta,
    ] if x)
    return {
        "url": r.get("html_url", ""),
        "title": r.get("full_name") or r.get("name", ""),
        "text": text,
        "kind": "repo",
        "antipode_eligible": False,
        "summary": summary,
        "summary_from": "description",
        "source": "github",
        "collected_by": f"collect_account.py:{how}",
        "full_text": text[:6000],
        "text_chars": len(text),
    }


# ---------------------------------------------------------------------------
# 3. 浏览器采集：登录 → 滚动 → 抽链接
# ---------------------------------------------------------------------------
_EXTRACT_JS = """
(() => {
  const SEL = __SEL__;
  const MAX = __MAX__;
  // 用字符串构造正则：直接把 pattern 写成 /.../ 字面量会因为里面有斜杠而语法错
  const NAV = new RegExp(__NAVSRC__, 'i');
  const out = [], seen = new Set();
  document.querySelectorAll(SEL).forEach(el => {
    let href = el.href || el.getAttribute('href') || '';
    if (!href || !/^https?:/i.test(href)) return;
    href = href.split('#')[0];
    if (NAV.test(new URL(href).pathname)) return;
    if (seen.has(href)) return;
    seen.add(href);
    // 标题通常不在 a 自己身上，往上找一个像"一个条目"的容器
    const box = el.closest('li, article, [class*="item"], [class*="card"], [class*="note"], [class*="feed"], [class*="post"]') || el.parentElement || el;
    const text = (box.innerText || el.innerText || el.getAttribute('aria-label') || '')
                   .replace(/\\s+/g, ' ').trim();
    out.push({ href, text: text.slice(0, 500) });
  });
  return out.slice(0, MAX);
})()
"""


def fill_credentials(b, username: str, password: str) -> str:
    """把账号密码填进当前页面的登录框，然后交还给人点"登录"。

    返回值是给人看的一句话（填上了几个框）。
    """
    js = """
    (() => {
      const u = %s, p = %s;
      const vis = el => el && el.offsetParent !== null;
      const users = [...document.querySelectorAll(
        'input[type=text], input[type=email], input[type=tel], input[name*=user i], input[name*=account i], input[name*=login i]'
      )].filter(vis);
      const pwds = [...document.querySelectorAll('input[type=password]')].filter(vis);
      if (!pwds.length) return {ok: false, why: '当前页面没有密码框（可能还没跳到登录页）'};
      const set = (el, val) => {
        if (!el) return;
        const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
        d.set.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      set(users[0], u);
      set(pwds[0], p);
      return {ok: true, filled: users.length ? 2 : 1, hasUser: !!users.length};
    })()
    """ % (json.dumps(username), json.dumps(password))
    r = b.eval_js(js) or {}
    if not r.get("ok"):
        return r.get("why") or "没找到登录框"
    return f"已填入{'账号和密码' if r.get('hasUser') else '密码'}，请点「登录」并在窗口里完成验证"


def collect_via_browser(url: str, platform: str, a) -> tuple[list[dict], str]:
    """打开浏览器 → 等登录 → 滚动 → 抽链接。返回 (原始条目, cookie 串)。"""
    from browser_cdp import Browser

    b = Browser(profile_dir=a.profile or "", headless=a.headless)
    b.start()
    try:
        final = b.goto(url, wait=1.5)
        print(f"  已打开：{final[:80]}")

        # --- 登录 ---
        if a.username or a.password:
            msg = fill_credentials(b, a.username or "", a.password or "")
            print(f"  {msg}")

        if not a.no_wait:
            print()
            print("  ┌────────────────────────────────────────────────────────┐")
            print("  │ 在刚弹出的浏览器窗口里登录（验证码 / 短信都在那儿完成） │")
            print("  │ 登录好、能看到你的内容列表之后，回到这里按回车         │")
            print("  └────────────────────────────────────────────────────────┘")
            try:
                input("  回车继续 > ")
            except EOFError:
                pass
            b.goto(url, wait=1.5)

        # 登录后可能被重定向到主页，重新导航一次确保在目标页
        cur = b.eval_js("location.href") or ""
        if urlparse(cur).hostname != urlparse(url).hostname:
            b.goto(url, wait=1.5)

        # --- 滚到底，把懒加载的都逼出来 ---
        if a.scroll:
            h = b.scroll(a.scroll, a.scroll_pause)
            print(f"  滚动 {a.scroll} 次，页面高度 {h}px")

        try:
            title = b.eval_js("document.title") or ""
        except Exception:                                      # noqa: BLE001
            title = ""
        logged_out = _looks_logged_out(b)
        if logged_out:
            print("  [!] 页面看起来还没登录（看到了登录按钮），抽出来的可能只有公开内容")

        sel = CONTENT_SELECTOR.get(platform, CONTENT_SELECTOR["generic"])
        # 多抽一些再筛：generic 选择器会把导航链接也抽进来
        js = (_EXTRACT_JS
              .replace("__SEL__", json.dumps(sel))
              .replace("__MAX__", str(max(60, (a.limit or 20) * 4)))
              .replace("__NAVSRC__", json.dumps(NAV_LIKE.pattern)))
        rows = b.eval_js(js) or []

        # 平台改版是常态：专用选择器抽不到时，退回"所有链接 + 长度过滤"再试一次。
        # 宁可多抽几条让人自己看，也不要因为一个 CSS 类名变了就整条链路失效。
        if not rows and platform != "generic":
            js2 = (_EXTRACT_JS
                   .replace("__SEL__", json.dumps("a[href]"))
                   .replace("__MAX__", str(max(60, (a.limit or 20) * 4)))
                   .replace("__NAVSRC__", json.dumps(NAV_LIKE.pattern)))
            rows = [r for r in (b.eval_js(js2) or []) if len(r.get("text") or "") >= 8]
            if rows:
                print("  （平台专用选择器没抽到，已退回通用选择器 —— 平台可能改版了）")

        print(f"  页面「{title[:30]}」抽到 {len(rows)} 个链接")
        if not rows:
            try:
                head = b.eval_js("(document.body && document.body.innerText || '').slice(0,400)") or ""
            except Exception:                                  # noqa: BLE001
                head = ""
            reason = _block_reason(title + " " + head)
            if reason:
                print(f"  [x] 被平台拦了：{reason}")
                print("      无头模式（--headless）几乎必然被拦 —— 这是实测过的，不是代码问题。")
                print("      正确做法：去掉 --headless，用有头窗口手动过验证码；")
                print("      再加 --profile <目录> 固定登录态，下次就不用重登。")
            else:
                print("  [!] 一个都没抽到。可能：没登录 / 选择器过时 / 内容需要点进二级页")

        # 只留同域名下的
        host = urlparse(url).hostname or ""
        items: list[dict] = []
        for r in rows:
            h = urlparse(r.get("href", "")).hostname or ""
            if h and h.split(".")[-2:] != host.split(".")[-2:]:
                continue
            text = (r.get("text") or "").strip()
            items.append({
                "url": r["href"],
                "title": _first_line(text),
                "hint_text": text,
            })

        cookie = b.cookie_header(host)
        return items, cookie
    finally:
        b.close()


BLOCK_HINTS = (
    ("验证码", "弹了验证码"),
    ("安全验证", "要求安全验证"),
    ("人机验证", "要求人机验证"),
    ("滑块", "要求滑块验证"),
    ("访问异常", "判定为异常访问"),
    ("异常流量", "判定为异常流量"),
    ("请完成验证", "要求完成验证"),
    ("captcha", "弹了验证码"),
    ("unusual traffic", "判定为异常流量"),
)


def _block_reason(text: str) -> str:
    """页面看起来是不是被风控拦了。只在"一条都没抽到"时才问，避免误报。"""
    low = (text or "").lower()
    for hint, why in BLOCK_HINTS:
        if hint.lower() in low:
            return why
    return ""


def _looks_logged_out(b) -> bool:
    try:
        return bool(b.eval_js(
            "/(登录|登入|Sign in|Log in)/.test("
            "(document.body.innerText||'').slice(0,400))"
        ))
    except Exception:                                          # noqa: BLE001
        return False


def _first_line(text: str, n: int = 70) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        return ""
    # 取第一个句子（中文句号/换行/感叹），没有就截断
    m = re.match(r"^(.{4,%d}?)[。！？!?\n]" % n, t)
    return (m.group(1) if m else t[:n]).strip()


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="客户给一个主页网址 + 账号凭据 → 建库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python first/code/collect_account.py --url https://github.com/ciqiongovo --token ghp_xxx",
    )
    ap.add_argument("--url", required=True, help="主页网址（账号主页 / 收藏页 / 项目页）")
    ap.add_argument("--platform", default="", help="平台（默认按域名自动判定）")

    # 认证
    ap.add_argument("--token", default="", help="GitHub PAT（只读即可）")
    ap.add_argument("--cookie", default="", help="会话 Cookie 串：k=v; k2=v2")
    ap.add_argument("--cookie-file", default="", help="从文件读 Cookie")
    ap.add_argument("--browser", action="store_true", help="开浏览器窗口，你在里面登录")
    ap.add_argument("--username", default="", help="配合 --browser：预填账号（不落盘、不打印）")
    ap.add_argument("--password", default="", help="配合 --browser：预填密码（不落盘、不打印）")
    ap.add_argument("--headless", action="store_true", help="无头（要求已有登录态，不弹窗）")
    ap.add_argument("--profile", default="", help="固定浏览器 profile 目录，登录态可跨次复用")
    ap.add_argument("--no-wait", action="store_true", help="不等人按回车（已登录时用）")
    ap.add_argument("--scroll", type=int, default=6, help="往下滚几次触发懒加载")
    ap.add_argument("--scroll-pause", type=float, default=1.2, help="每滚一次停多久")

    # 范围
    ap.add_argument("--scope", default="starred", choices=["starred", "repos", "issues"],
                    help="GitHub 取什么（其它平台不用这个）")
    ap.add_argument("--limit", type=int, default=20, help="最多几条")
    ap.add_argument("--vault-id", default="", help="库 ID")
    ap.add_argument("--topic", default="", help="这个库是什么主题")
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "data"))
    ap.add_argument("--antipode", choices=["on", "off"], default="on",
                    help="这个库要不要开反推荐（GitHub 这类建议 off）")
    ap.add_argument("--save-cookies", action="store_true",
                    help="把登录态存进 cookies.json（已在 .gitignore 里），后续抓正文也能用")
    ap.add_argument("--cookies-out", default=os.path.join(_HERE, "cookies.json"))
    ap.add_argument("--no-model", action="store_true", help="不调模型，只抓正文 + 抽取式摘要")
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--min-chars", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=20.0)
    a = ap.parse_args(argv)

    platform = a.platform or detect_platform(a.url)
    vault_id = a.vault_id or f"{platform}-{_now()[5:16].replace(':', '').replace('-', '')}"

    print("=" * 78)
    print(f"采集   平台 {platform}   网址 {a.url[:70]}")
    print("=" * 78)

    # ---------- GitHub：走 API ----------
    if platform == "github":
        if not a.token:
            print("❌ GitHub 需要一个只读 token：https://github.com/settings/tokens", file=sys.stderr)
            print("   （勾 repo 的读权限即可；这不是密码，随时可以吊销）", file=sys.stderr)
            return 2
        try:
            raw = collect_github(a.url, a.token, a.scope, a.limit)
        except urllib.error.HTTPError as e:
            print(f"❌ GitHub 返回 {e.code}：{e.reason}", file=sys.stderr)
            if e.code == 401:
                print("   token 无效或过期了", file=sys.stderr)
            return 3
        print(f"  取到 {len(raw)} 条（{a.scope}）")
        items = raw
        cookie = ""

    # ---------- 其它：浏览器 ----------
    else:
        cookie = a.cookie
        if a.cookie_file:
            with open(a.cookie_file, "r", encoding="utf-8", errors="replace") as fh:
                cookie = fh.read().strip()

        if not (a.browser or a.cookie or cookie):
            print("❌ 这个平台要登录才能看到列表。三种给法任选一种：", file=sys.stderr)
            print("   --browser                       开窗口登录（推荐，能过验证码）", file=sys.stderr)
            print("   --cookie 'k=v; k2=v2'           从浏览器复制 Cookie", file=sys.stderr)
            print("   --cookie-file cookies.json      从文件读", file=sys.stderr)
            return 2

        if a.browser:
            raw, cookie = collect_via_browser(a.url, platform, a)
        else:
            # 只有 Cookie、不开浏览器：带着登录态抓一次 HTML，从里面挑链接
            raw = collect_static(a.url, cookie, platform, a.limit)

        # 登录态交给 vault_fetch（它按 ANTIPODE_COOKIES 读文件），
        # 下面逐条抓正文就带着登录态了 —— 抖音/微博的详情页没登录根本看不了。
        if cookie:
            os.environ["ANTIPODE_COOKIES"] = _cookie_env(cookie, platform, a)

        if a.limit:
            raw = raw[: a.limit]        # 逐条抓正文很贵，先截断再处理

        opts = make_opts(timeout=a.timeout, max_chars=a.max_chars, min_chars=a.min_chars,
                         no_model=a.no_model)
        items = []
        for n, r in enumerate(raw, 1):
            # 列表页已经给了标题/片段：抓不到正文时用它兜底，别让信息全丢
            pre = {"title": r.get("title", ""), "text": r.get("hint_text", "")}
            it = build_item(r["url"], None if a.no_model else _backend(), opts, prefetched=pre)
            it["collected_by"] = f"collect_account.py:{platform}"
            if r.get("hint_text") and it.get("kind") in ("unreachable", "too_short"):
                it["summary"] = _first_line(r["hint_text"], 90)
                it["summary_from"] = "list_page"
            items.append(it)
            flag = "★" if it.get("antipode_eligible") else " "
            print(f"  {flag} {n:>2}. {(it.get('title') or r.get('title') or '(无标题)')[:32]:<34}"
                  f" {it.get('kind', ''):<12} {it.get('text_chars', 0):>5}字")

    items = _dedupe_items(items)
    if a.limit:
        items = items[: a.limit]

    ok = [i for i in items if not i.get("error")]
    eligible = [i for i in items if i.get("antipode_eligible")]
    kinds: dict[str, int] = {}
    for i in items:
        kinds[i.get("kind", "?")] = kinds.get(i.get("kind", "?"), 0) + 1

    vault = {
        "vault_id": vault_id,
        "topic": a.topic,
        "antipode_enabled": a.antipode == "on",
        "created_at": _now(),
        "built_by": "collect_account.py",
        "source": f"account:{platform}",
        "account": {
            "platform": platform,
            "url": a.url,
            "scope": a.scope if platform == "github" else "profile",
            "auth": "token" if platform == "github" else ("browser" if a.browser else "cookie"),
        },
        "item_count": len(items),
        "antipode_eligible_count": len(eligible),
        "kinds": kinds,
    }
    vpath, ipath = write_vault(vault, items, a.out_dir)

    print("-" * 78)
    print(f"  入库 {len(ok)} / {len(items)} 条")
    print("  类型：" + "  ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))
    print(f"  ★ 可参与反推荐：{len(eligible)} 条")
    print()
    print(f"  → {os.path.relpath(vpath, _ROOT)}")
    print(f"  → {os.path.relpath(ipath, _ROOT)}")
    if cookie and a.save_cookies:
        print("  登录态已存入 cookies.json（已 gitignore）；后续抓正文会自动带上")
    print()
    print("  下一步：")
    print(f"    python first/code/run_vault.py --vault {os.path.relpath(vpath, _ROOT)}")
    return 0


_backend_cache: list = []


def _backend():
    if not _backend_cache:
        from model_adapter import OpenAICompatBackend
        _backend_cache.append(OpenAICompatBackend())
    return _backend_cache[0]


_TEMP_FILES: list[str] = []


def _cookie_env(cookie: str, platform: str, a) -> str:
    """把 Cookie 变成一个文件路径，交给 vault_fetch 读。

    只有加了 `--save-cookies` 才落到 cookies.json（可跨次复用）；
    否则写临时文件、退出时删掉 —— 没打招呼就把别人的登录态留在磁盘上不合适。
    """
    if getattr(a, "save_cookies", False):
        return _persist_cookie(cookie, platform, a)

    import tempfile

    fd, path = tempfile.mkstemp(prefix="antipode-ck-", suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({_cookie_key(platform): cookie}, fh)
    _TEMP_FILES.append(path)
    return path


def _cleanup_temp() -> None:
    for p in _TEMP_FILES:
        try:
            os.remove(p)
        except Exception:                                      # noqa: BLE001
            pass


def _persist_cookie(cookie: str, platform: str, a) -> str:
    """把登录态落到 cookies.json（gitignore），并用环境变量指过去。"""
    path = a.cookies_out
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except Exception:                                      # noqa: BLE001
            data = {}
    data[_cookie_key(platform)] = cookie
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        try:                                                   # 只给自己读写
            os.chmod(path, 0o600)
        except Exception:                                      # noqa: BLE001
            pass
    except Exception as e:                                     # noqa: BLE001
        print(f"  [!] Cookie 存不下来（不影响本次）：{e}", file=sys.stderr)
    return path


def _cookie_key(platform: str) -> str:
    return {
        "douyin": "douyin.com",
        "weibo": "weibo.com",
        "bilibili": "bilibili.com",
        "zhihu": "zhihu.com",
        "xiaohongshu": "xiaohongshu.com",
    }.get(platform, platform)


def collect_static(url: str, cookie: str, platform: str, limit: int) -> list[dict]:
    """不开浏览器：带 Cookie 抓一次 HTML，从里面挑链接。

    只对"服务端就把列表渲染出来"的站点管用（微博的 m 站、部分博客）。
    SPA 站点（抖音）这样抓是空的 —— 那就得用 --browser，别在这上面浪费时间。
    """
    from vault_fetch import _http, _site_of

    status, body, final, _h = _http(url, timeout=20)
    html = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    host = urlparse(url).hostname or ""
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = f"https://{host}{href}"
        if not href.startswith("http"):
            continue
        if urlparse(href).hostname != host:
            continue
        if NAV_LIKE.search(urlparse(href).path):
            continue
        if _norm_url(href) in seen:
            continue
        seen.add(_norm_url(href))
        out.append({"url": href, "title": _first_line(inner), "hint_text": re.sub(r"\s+", " ", inner).strip()})
        if len(out) >= limit:
            break
    _ = (status, final, cookie, platform, _site_of)
    return out


def _dedupe_items(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        k = _norm_url(it.get("url", ""))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


if __name__ == "__main__":
    import atexit

    atexit.register(_cleanup_temp)
    sys.exit(main())
