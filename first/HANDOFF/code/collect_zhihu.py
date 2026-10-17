#!/usr/bin/env python3
"""知乎：摘一批信息 → 去掉广告和网文 → 判断哪几条值得做反推荐 → 建库。

和 GitHub 那条线的区别
--------------------
GitHub 的仓库没有立场，所以**只做信息收集**。
知乎有大量观点长文，所以这条路是「信息收集 + 有条件反推荐」：

    摘 10 条 ──▶ 垃圾过滤（广告/盐选小说/空壳）──▶ 判立场
              ──▶ 按「是否适合反推荐」分两堆 ──▶ 适合的才跑反引擎

为什么要"判断是否适合"而不是每条都跑
--------------------------------
给一篇《如何评价某手机》找对立面是合理的；给一条「XX 品牌广告」或一篇
盐选小说找对立面则是荒谬的。而且反推荐要真的去检索、真的调模型，
每条都跑既慢又费钱，还会让反库里混进噪声。所以准入只留给**有明确立场、
有论证结构**的那些，其余明确记下原因、直接放弃。

三步准入（依次收紧）
------------------
  1. `junk_filter`：广告 / 付费小说 / 空壳 —— 规则判，可解释
  2. `build_vault.build_item`：抓正文，看有没有论证结构、有没有明确立场
  3. `antipode_eligible`：前两步都过，才允许进反引擎

用法
----
    # 弹窗登录（第一次），然后摘推荐流前 10 条
    python collect_zhihu.py --limit 10 --browser

    # 已有登录态，连着跑反推荐（适合的那几条）
    python collect_zhihu.py --limit 10 --browser --antipode

    # 只建库不跑模型（测链路）
    python collect_zhihu.py --limit 5 --browser --no-model
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIRST = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from antipode_sources import strip_html                          # noqa: E402
from build_vault import _now, build_item, make_opts, write_vault  # noqa: E402
from collect_account import _backend, _cookie_env, _first_line          # noqa: E402
from retrieval_terms import ISSUE_TERMS                          # noqa: E402
import junk_filter                                              # noqa: E402

_LABEL = "知乎"


# ---------------------------------------------------------------------------
# 1. 在信息流里抽取候选
# ---------------------------------------------------------------------------
# 知乎的卡片 class 换过好几轮，所以**不依赖单一选择器**：
# 先按链接特征捞（问答 / 专栏 / 站外跳转 / 付费墙），再往上找最近的容器。
# 容器找不到就用链接本身的文字 —— 宁可信息少一点，也不能整页抽空。
_EXTRACT_JS = r"""
(() => {
  const MAX = __MAX__;
  const RES = /\/question\/\d+|zhuanlan\.zhihu\.com\/p\/|link\.zhihu\.com|\/market\//i;
  const out = [];
  const seen = new Set();

  const isAdBox = (el) => {
    if (!el || !el.className) {
      const m = el && el.getAttribute && el.getAttribute('data-za-detail-view-path-module');
      return !!m && /AdItem/i.test(m);
    }
    const s = String(el.className) + ' ' +
              (el.getAttribute ? (el.getAttribute('data-za-detail-view-path-module') || '') : '');
    return /Pc-feedAd|AdItem|ad-item|Advert/i.test(s);
  };

  const links = Array.from(document.querySelectorAll('a[href]'));
  for (const a of links) {
    if (out.length >= MAX) break;
    const href = a.href || '';
    if (!RES.test(href)) continue;
    if (seen.has(href)) continue;

    // 往上找卡片容器（最多 6 层，够覆盖各家改版）
    let box = a;
    let adBox = false;
    for (let i = 0; i < 6 && box; i++) {
      if (isAdBox(box)) adBox = true;
      if (box.matches && box.matches('.Feed-item, .TopstoryItem, .List-item, article, .Card')) break;
      box = box.parentElement;
    }
    const container = box || a.parentElement;
    const text = (container && container.innerText || a.innerText || '')
                   .replace(/\s+/g, ' ').trim();

    // 广告角标：卡片里单独挂着「广告」两个字的元素
    let adLabel = adBox;
    if (container && container.querySelector) {
      const hit = Array.from(container.querySelectorAll('span, div'))
        .some(e => (e.innerText || '').trim() === '广告');
      adLabel = adLabel || hit;
    }

    const title = (a.innerText || '').replace(/\s+/g, ' ').trim()
                  || (_first_line_from(text));
    seen.add(href);
    out.push({
      href: href,
      title: title.slice(0, 200),
      text: text.slice(0, 600),
      ad: adLabel
    });
  }
  return out;

  function _first_line_from(s) {
    const i = String(s || '').indexOf(' ');
    return i > 8 ? String(s).slice(0, i) : String(s || '').slice(0, 80);
  }
})()
"""


def extract_rows(b, limit: int, scroll: int) -> list[dict]:
    """滚动触发懒加载，然后抽取。"""
    total = 0
    for i in range(max(scroll, 0)):
        total = b.scroll(times=1, pause=0.8) or total
    js = _EXTRACT_JS.replace("__MAX__", str(max(limit * 4, 20)))
    rows = b.eval_js(js) or []
    print(f"  滚了 {scroll} 次，页面高度 {total}px，抽到 {len(rows)} 个链接")
    return rows


# ---------------------------------------------------------------------------
# 2. 在浏览器里取正文（这是关键一步）
# ---------------------------------------------------------------------------
# 为什么不能用 urllib 直接抓
# ------------------------
# 实测：ug 抓知乎详情页会稳定返回 `HTTP 403（反爬/需登录）`，10 条全中。
# 知乎的正文接口要求登录态，而登录态在浏览器里（含 HttpOnly Cookie）。
# 所以改成**在页面上下文里 fetch** —— 同源、自动带 Cookie、不碰密码，
# 拿到的还是结构化 JSON，比解析 DOM 干净得多，也不用担心改版。
#
# 三种链接对应三个接口：
#   /question/{q}/answer/{a}  → /api/v4/answers/{a}      单条回答
#   zhuanlan.zhihu.com/p/{id} → /api/v4/articles/{id}    专栏文章
#   /question/{q}             → /api/v4/questions/{q}/answers   取高赞回答
_RE_ANSWER = re.compile(r"/answer/(\d+)")
_RE_ARTICLE = re.compile(r"zhuanlan\.zhihu\.com/p/(\d+)")
_RE_QUESTION = re.compile(r"/question/(\d+)")

_API_JS = r"""
(async () => {
  try {
    const r = await fetch(__URL__, {
      credentials: 'include',
      headers: { 'accept': 'application/json, text/plain, */*' }
    });
    if (!r.ok) return { ok: false, error: 'HTTP ' + r.status };
    return { ok: true, data: await r.json() };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
})()
"""


def _api_for(url: str) -> tuple[str, str]:
    """把一条知乎链接翻译成「该去哪个接口取正文」。"""
    m = _RE_ANSWER.search(url)
    if m:
        return (f"https://www.zhihu.com/api/v4/answers/{m.group(1)}"
                f"?include=content,question.title"), "answer"
    m = _RE_ARTICLE.search(url)
    if m:
        return (f"https://www.zhihu.com/api/v4/articles/{m.group(1)}"
                f"?include=content,title"), "article"
    m = _RE_QUESTION.search(url)
    if m:
        return (f"https://www.zhihu.com/api/v4/questions/{m.group(1)}/answers"
                f"?include=content&limit=3&sort_by=default"), "question"
    return "", ""


def _text_from_payload(kind: str, data: dict) -> tuple[str, str]:
    """从接口返回里取出（正文纯文本, 标题）。

    问题页会返回多个回答：取**最长**那条。不是偏爱长文，而是短回答常常
    只有一句情绪，没有论证结构，判立场时会全军覆没；长的那条至少大概率
    把理由写出来了。
    """
    if kind == "answer":
        body = strip_html(data.get("content") or "")
        title = ((data.get("question") or {}).get("title") or "").strip()
        return body, title
    if kind == "article":
        return strip_html(data.get("content") or ""), (data.get("title") or "").strip()
    if kind == "question":
        best = ""
        for ans in (data.get("data") or []):
            t = strip_html(ans.get("content") or "")
            if len(t) > len(best):
                best = t
        return best, ""
    return "", ""


# 专栏文章走另一条路：直接开页面读 DOM。
# 为什么？专栏的接口在跨子域时会 403/404（实测 `www.zhihu.com/api/v4/articles/{id}`
# → 403；切到 zhuanlan 子域再调 → 404），但**页面本身**登录后是完整渲染的，
# `.Post-RichText` 里有 1 万字正文。所以专栏不折腾接口，直接读渲染结果。
_ARTICLE_JS = r"""
(() => {
  // 知乎的 document.title 会带上未读私信提醒，形如
  //   "(12 封私信 / 14 条消息) 微服务入门这一篇就够了 - 知乎"
  // 直接拿来当标题，演示页上就很难看。这里剥掉前缀和站名后缀。
  const clean = (t) => String(t || '')
      .replace(/^\(\s*\d+\s*封私信[^)]*\)\s*/, '')
      .replace(/\s*-\s*知乎\s*$/, '')
      .trim();

  const sels = ['.Post-RichText', '.RichText', '.Post-content', 'article'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && el.innerText && el.innerText.length > 100) {
      return { sel: s, text: el.innerText, title: clean(document.title) };
    }
  }
  const body = document.body ? document.body.innerText : '';
  return { sel: '', text: body, title: clean(document.title) };
})()
"""


def fetch_bodies(b, rows: list[dict], max_chars: int = 20000) -> dict[str, dict]:
    """逐条取正文。返回 {url: {ok, text, title, chars, method, error}}。

    分两批跑，不是图省事：
      · 回答 / 问题 → 在 **www.zhihu.com 源**上调接口（跨域会被拦，不能乱切页面）
      · 专栏文章   → 直接开页面读 DOM
    混着跑会导致 fetch 发到跨域地址，拿回一堆 403，白等十几秒。
    """
    out: dict[str, dict] = {}
    if not rows:
        return out

    api_rows: list[tuple[int, dict, str, str]] = []
    art_rows: list[tuple[int, dict]] = []
    for n, r in enumerate(rows, 1):
        api, kind = _api_for(r.get("href", ""))
        if kind == "article":
            art_rows.append((n, r))
        elif api:
            api_rows.append((n, r, api, kind))
        else:
            out[r.get("href", "")] = {"ok": False, "error": "不认识的知乎链接形态"}
            print(f"  {n:>2}/{len(rows)}  -             不认识的链接形态  {r.get('href','')[:38]}")

    total = len(rows)
    if api_rows:
        print(f"  取正文 · 回答/问题 {len(api_rows)} 条（页面会话里的接口）…")
    for n, r, api, kind in api_rows:
        url = r.get("href", "")
        try:
            res = b.eval_js(_API_JS.replace("__URL__", json.dumps(api)), timeout=25)
        except Exception as e:                                 # noqa: BLE001
            out[url] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
            print(f"  {n:>2}/{total}  x 取正文失败   {url[:44]}")
            continue
        if not res or not res.get("ok"):
            err = str((res or {}).get("error", "无返回"))[:120]
            out[url] = {"ok": False, "error": err}
            print(f"  {n:>2}/{total}  x {err[:20]:<20} {url[:40]}")
            continue
        text, title = _text_from_payload(kind, res.get("data") or {})
        ok = len(text) >= 120
        out[url] = {"ok": ok, "text": text[:max_chars], "title": title,
                    "chars": len(text), "method": "browser_api", "kind": kind,
                    "error": "" if ok else f"接口只返回 {len(text)} 字"}
        print(f"  {n:>2}/{total}  {'√' if ok else 'x'} {len(text):>6}字       {url[:40]}")

    for n, r in art_rows:
        url = r.get("href", "")
        try:
            b.goto(url, wait=2.0)
            got = b.eval_js(_ARTICLE_JS) or {}
        except Exception as e:                                 # noqa: BLE001
            out[url] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
            print(f"  {n:>2}/{total}  x 打开失败     {url[:44]}")
            continue
        text = (got.get("text") or "").strip()
        ok = len(text) >= 120
        out[url] = {"ok": ok, "text": text[:max_chars], "title": (got.get("title") or ""),
                    "chars": len(text), "method": "browser_dom", "kind": "article",
                    "error": "" if ok else f"页面只读到 {len(text)} 字"}
        print(f"  {n:>2}/{total}  {'√' if ok else 'x'} {len(text):>6}字       {url[:40]}")

    good = sum(1 for v in out.values() if v.get("ok"))
    print(f"  取到正文 {good} 条 / 共 {total} 条")
    return out


# ---------------------------------------------------------------------------
# 3. 等登录
# ---------------------------------------------------------------------------
def resolve_query(a) -> str:
    """把 --issue / --query 翻译成一个搜索关键词。

    为什么需要「按议题采集」
    --------------------
    热榜是**综合时事**（票房、欠薪、博物馆闭馆），跟我们那 16 个二元议题
    （微服务 vs 单体、远程 vs 坐班…）一条都不沾边 —— 实测 10 条全被议题依据闸门
    拦下，一条反信息都产不出。这不是闸门错了，是入口选错了：给一条时政新闻找
    「技术选型的对立面」，本来就无从下手。

    所以采集入口分两种，各司其职：
      · 热榜 / 推荐流 → **信息收集**（同 GitHub 那条线）：省流 + 原文地址，不硬凑反面
      · 按议题搜索   → **信息收集 + 反推荐**：摘到的内容天然属于某场争论
    """
    q = (getattr(a, "query", "") or "").strip()
    if q:
        return q
    iid = (getattr(a, "issue", "") or "").strip()
    if not iid:
        return ""
    entry = ISSUE_TERMS.get(iid)
    if not entry:
        print(f"  [!] 议题表里没有 {iid}，退回热榜。可用议题：{', '.join(list(ISSUE_TERMS)[:6])} …")
        return ""
    topics = entry.get("topics") or []
    return topics[0] if topics else ""


def open_and_wait(b, a) -> bool:
    query = resolve_query(a)
    if query:
        start = ("https://www.zhihu.com/search?type=content&q="
                 + quote(query))
        where = f"搜索「{query}」"
    elif getattr(a, "scope", "hot") == "hot":
        start, where = "https://www.zhihu.com/hot", "热榜"
    else:
        start, where = "https://www.zhihu.com/", "推荐流"
    print(f"\n  正在打开 {_LABEL}（{where}）…", flush=True)
    try:
        b.goto(start, wait=2.5)
    except Exception as e:                                     # noqa: BLE001
        print(f"  [!] 打开失败：{e}")

    print()
    print("  " + "─" * 58)
    print("  在这个窗口里登录知乎（扫码最快）。验证码、短信都由你自己过。")
    print("  登录完停在这个页面就行，程序会自己发现。")
    print("  " + "─" * 58)

    t0 = time.time()
    tick = t0
    while time.time() - t0 < a.wait:
        try:
            href = b.eval_js("location.href") or ""
            n = b.eval_js(
                "document.querySelectorAll("
                "'a[href*=\"/question/\"], a[href*=\"zhuanlan.zhihu.com/p/\"]')"
                ".length") or 0
        except Exception:                                      # noqa: BLE001
            href, n = "", 0

        if "signin" not in href and n >= 3:
            print(f"\n  [√] 已登录：{href[:70]}（页面有 {n} 条内容链接）")
            return True

        if time.time() - tick > 12:
            tick = time.time()
            left = int(a.wait - (time.time() - t0))
            where = "登录页" if "signin" in href else (urlparse(href).path or "/")
            print(f"  … 还在等登录（剩 {left}s）  当前：{where[:40]}  链接 {n} 条")
        time.sleep(1.5)

    print("\n  [x] 没等到登录。再跑一次就行，登录态会存在 first/.profiles/zhihu。")
    return False


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
def filter_rows(rows: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    """垃圾过滤。返回（留下的, 丢掉的含原因）。

    「丢掉」不是消失：每条都带 reason 写进 *.junk.jsonl，
    日报和答辩时能直接说清"这条为什么没进库"。
    """
    print()
    print("  " + "-" * 66)
    print("  垃圾过滤")
    print("  " + "-" * 66)
    kept: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        url = r.get("href", "")
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        verdict = junk_filter.classify(
            url=url, title=r.get("title", ""), text=r.get("text", ""),
            dom_is_ad=bool(r.get("ad")),
        )
        if verdict["drop"]:
            dropped.append({"url": url, "title": r.get("title", ""), **verdict})
            print(f"  ✗ {(r.get('title') or url)[:34]:<36} "
                  f"{verdict['reason']}")
        else:
            kept.append(r)
        if len(kept) >= limit:
            break
    print(f"\n  通过 {len(kept)} 条，丢弃 {len(dropped)} 条")
    return kept, dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="知乎：摘信息 → 去垃圾 → 判可反推荐性 → 建库")
    ap.add_argument("--limit", type=int, default=10, help="最多几条（默认 10）")
    ap.add_argument("--scroll", type=int, default=6, help="往下滚几次")
    ap.add_argument("--scope", default="hot", choices=("hot", "feed"),
                    help="hot=热榜（默认，观点密度高）；feed=首页推荐流")
    ap.add_argument("--issue", default="",
                    help="按议题采集（议题 id，如 microservices_vs_monolith）")
    ap.add_argument("--query", default="",
                    help="按关键词采集（如「微服务」）；给了它就走搜索页")
    ap.add_argument("--wait", type=float, default=300.0, help="最长等登录多久（秒）")
    ap.add_argument("--profile", default="", help="登录态目录（默认 first/.profiles/zhihu）")
    ap.add_argument("--topic", default="", help="库的标题")
    ap.add_argument("--vault-id", default="")
    ap.add_argument("--antipode", action="store_true",
                    help="建库后，对「适合」的那几条跑反推荐")
    ap.add_argument("--no-model", action="store_true", help="不调模型（只测采集）")
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--min-chars", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out-dir", default=os.path.join(_FIRST, "data"))
    ap.add_argument("--keep-open", action="store_true")
    a = ap.parse_args(argv)

    from browser_cdp import Browser, has_websockets
    if not has_websockets():
        print("[x] 需要 websockets：pip install websockets", file=sys.stderr)
        return 3

    profile = a.profile or os.path.join(_FIRST, ".profiles", "zhihu")
    os.makedirs(profile, exist_ok=True)

    print()
    print("=" * 70)
    print(f"  {_LABEL} · 摘信息 → 去垃圾 → 判可反推荐性")
    print("=" * 70)

    b = Browser(profile_dir=profile, headless=False, keep_open=a.keep_open)
    try:
        b.start()
        if not open_and_wait(b, a):
            return 4
        rows = extract_rows(b, a.limit, a.scroll)
        if not rows:
            print("  [x] 一条都没抽到。可能是页面还没加载完，把 --scroll 调大再试。")
            return 5

        kept, dropped = filter_rows(rows, a.limit)
        # 正文必须在**关浏览器之前**取：登录态在浏览器里，关掉就没了。
        bodies = fetch_bodies(b, kept, max_chars=a.max_chars * 4) if kept else {}
        cookie = ""
        try:
            cookie = b.cookie_header("www.zhihu.com")
        except Exception:                                      # noqa: BLE001
            pass
    finally:
        b.close()

    if not kept:
        print("  [x] 全被过滤掉了。可能是信息流里广告太多，多滚几次换个时段再试。")
        return 6

    # ---- 建库（判立场 + 概括）--------------------------------------------
    if cookie:
        os.environ["ANTIPODE_COOKIES"] = _cookie_env(cookie, "zhihu", a)

    opts = make_opts(timeout=a.timeout, max_chars=a.max_chars,
                     min_chars=a.min_chars, no_model=a.no_model)
    backend = None if a.no_model else _backend()

    print()
    print("  " + "-" * 66)
    print("  判立场（这一步决定哪几条能做反推荐）")
    print("  " + "-" * 66)
    items: list[dict] = []
    for n, r in enumerate(kept, 1):
        url = r["href"]
        body = bodies.get(url) or {}
        # 用浏览器取到的正文当 prefetched：build_item 见它够长就不再自己抓，
        # 也就不会再撞上那个必然 403 的 HTTP 抓取路径。
        pre = {
            "title": (body.get("title") or r.get("title") or "").strip(),
            "text": (body.get("text") or r.get("text") or "").strip(),
        }
        it = build_item(url, backend, opts, prefetched=pre)
        it["collected_by"] = "collect_zhihu.py"
        it["junk_check"] = "passed"
        if body.get("ok"):
            it["fetch_method"] = body.get("method", "browser_api")
            it["fetch_kind"] = body.get("kind", "")
        else:
            it["body_error"] = body.get("error", "取正文失败")
        # 正文没取到时，列表页摘要仍然当省流留着 —— 宁可信息少，不要整条空白
        if not body.get("ok") and r.get("text"):
            it["summary"] = _first_line(r["text"], 90)
            it["summary_from"] = "list_page"
        items.append(it)

        flag = "★" if it.get("antipode_eligible") else " "
        print(f"  {flag} {n:>2}. {(it.get('title') or '(无标题)')[:30]:<32}"
              f" {it.get('kind', ''):<12} {it.get('text_chars', 0):>5}字 "
              f"{it.get('summary', '')[:22]}")

    # ---- 收尾统计 --------------------------------------------------------
    eligible = [i for i in items if i.get("antipode_eligible")]
    kinds: dict[str, int] = {}
    for i in items:
        kinds[i.get("kind", "?")] = kinds.get(i.get("kind", "?"), 0) + 1

    vault_id = a.vault_id or f"zhihu-{_now()[5:16].replace(':', '').replace('-', '')}"
    scope_name = "search" if resolve_query(a) else ("hot" if a.scope == "hot" else "feed")
    default_topic = {
        "hot": "知乎 · 热榜",
        "feed": "知乎 · 今日信息流",
        "search": f"知乎 · 搜索「{resolve_query(a)}」",
    }[scope_name]
    vault = {
        "vault_id": vault_id,
        "topic": a.topic or default_topic,
        "antipode_enabled": bool(eligible),
        "counter_types": ["opposing_view"] if eligible else [],
        "created_at": _now(),
        "built_by": "collect_zhihu.py",
        "source": "account:zhihu",
        "account": {"platform": "zhihu", "url": "https://www.zhihu.com/",
                    "scope": scope_name, "query": resolve_query(a),
                    "issue": a.issue or "", "auth": "browser"},
        "item_count": len(items),
        "antipode_eligible_count": len(eligible),
        "kinds": kinds,
        "junk": {"dropped": len(dropped),
                 "reasons": _tally([d["junk"] for d in dropped])},
    }
    vpath, ipath = write_vault(vault, items, a.out_dir)
    jpath = os.path.join(a.out_dir, f"vault_{vault_id}.junk.jsonl")
    with open(jpath, "w", encoding="utf-8") as fh:
        for d in dropped:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    print()
    print("  " + "-" * 66)
    print(f"  入库 {len(items)} 条    ★ 适合反推荐 {len(eligible)} 条   "
          f"丢弃 {len(dropped)} 条")
    print("  类型：" + "  ".join(f"{k} {v}" for k, v in
                                 sorted(kinds.items(), key=lambda x: -x[1])))
    print(f"  → {os.path.relpath(vpath, _FIRST)}")
    print(f"  → {os.path.relpath(jpath, _FIRST)}   ← 被丢掉的及原因")
    print()

    if a.antipode and eligible and not a.no_model:
        print("  " + "-" * 66)
        print(f"  对 {len(eligible)} 条适合的跑反推荐 …")
        print("  " + "-" * 66)
        cmd = [sys.executable, os.path.join(_HERE, "run_vault.py"),
               "--vault", vpath, "--items", ipath]
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"  [!] 反推荐返回 {rc}，库已经建好了，可以单独重跑。")
    elif eligible and not a.antipode:
        print("  想接着跑反推荐，加 --antipode：")
        print(f"      python first/code/collect_zhihu.py --browser --antipode")
    return 0


def _tally(xs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main())
