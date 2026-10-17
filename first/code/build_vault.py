#!/usr/bin/env python3
"""客户给的链接 → 一个仓库（Vault）。

以前这一侧归队友（采集 / 筛选 / 概括 / 建库），现在归我们：
给一批链接，逐条**抓正文 → 判议题与立场 → 写一句省流 → 归档**，
产出一份可以直接喂给反引擎的库。

用法
----
    # 最常见：给一个 txt，一行一个链接
    python code/build_vault.py --urls links.txt --vault-id client-a --topic "前端选型"

    # 直接给链接（可重复）
    python code/build_vault.py --url https://a.com/1 --url https://b.com/2

    # 客户直接粘贴一段话，里面混着链接
    python code/build_vault.py --text "这几篇不错 https://a.com/1 还有 https://b.com/2"

    # 客户给的是一个列表页（专栏 / 收藏夹），从里面发现文章链接
    python code/build_vault.py --url https://blog.csdn.net/xxx --discover --limit 20

    # 不调模型（只要抓取 + 抽取式摘要，零成本）
    python code/build_vault.py --urls links.txt --no-model

产出
----
    data/vault_<vault_id>.json          库（vault_id / topic / antipode_enabled）
    data/vault_<vault_id>.items.jsonl   条目，一行一条，可直接给 run_vault.py

设计上的两条取舍
--------------
1. **省流版与原版都存**：`summary` 只用来让用户扫一眼，判立场必须用 `full_text`。
   省流版会把作者态度抹平 —— 这是实测过的。
2. **`antipode_eligible` 由这里打**：只有「在表达主张」的内容才值得找对立面。
   教程、文档、新闻通稿没有"反面观点"，硬找只会得到抬杠。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

from antipode_engine import (  # noqa: E402
    extractive_summary, is_opinion_post, is_tutorial_title, make_summary,
)
from model_adapter import OpenAICompatBackend  # noqa: E402
from stance_extractor import extract, extract_long  # noqa: E402
from retrieval_terms import NO_RETRIEVAL, topic_coverage  # noqa: E402
from vault_fetch import fetch_article, guess_source  # noqa: E402

CST = timezone(timedelta(hours=8))

URL_RE = re.compile(r"https?://[^\s<>\"'`）)】\]，。；、]+", re.I)

# 列表页里明显不是文章的东西
NON_ARTICLE = re.compile(
    r"/(tag|tags|category|categories|about|login|register|signup|search|user|users"
    r"|archive|archives|page|feed|rss|sitemap)(/|$|\?)", re.I
)

TUTORIAL_HINT = re.compile(
    r"教程|入门|从零|零基础|指南|手把手|how\s*to|getting\s*started|tutorial"
    r"|文档|官方文档|API\s*文档|速查|cheat\s*sheet|安装|配置说明",
    re.I,
)


# ---------------------------------------------------------------------------
# 1. 收集链接
# ---------------------------------------------------------------------------
def collect_urls(a) -> list[str]:
    """四种来源合并：--url / --urls / --text / stdin。"""
    found: list[str] = []
    for u in a.url or []:
        found.append(u.strip())

    if a.urls:
        path = a.urls
        if not os.path.exists(path) and os.path.exists(os.path.join(_ROOT, path)):
            path = os.path.join(_ROOT, path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read()
        # 一行一个 或 一整段里夹着链接，两种都认
        found.extend(URL_RE.findall(blob))

    if a.text:
        found.extend(URL_RE.findall(a.text))

    if a.stdin:
        found.extend(URL_RE.findall(sys.stdin.read()))

    return found


def _norm_url(u: str) -> str:
    """去 scheme / www / 末尾斜杠 / query，用来判"是不是同一篇"。"""
    p = urlparse(u.strip().lower())
    host = (p.hostname or "").lstrip("w.")
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path.rstrip('/')}"


def dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        k = _norm_url(u)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(u.strip())
    return out


def discover_links(page_url: str, limit: int = 20) -> list[str]:
    """从一个列表页里发现同域文章链接（客户给的是专栏 / 收藏夹时用）。

    只取同域、看起来像文章的链接，按出现顺序。宁少勿滥 —— 抓错了会污染库。
    """
    r = fetch_article(page_url, timeout=20)
    if not r.ok:
        print(f"  [!] 打不开列表页：{r.error}", file=sys.stderr)
        return []

    host = urlparse(page_url).hostname or ""
    links: list[str] = []
    seen: set[str] = set()
    for href in re.findall(r'<a[^>]+href="([^"]+)"', _raw_html_links(r)):
        u = href.strip()
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = f"https://{host}{u}"
        if not u.startswith("http"):
            continue
        if urlparse(u).hostname != host:
            continue
        if NON_ARTICLE.search(urlparse(u).path):
            continue
        if _norm_url(u) == _norm_url(page_url):
            continue
        k = _norm_url(u)
        if k in seen:
            continue
        seen.add(k)
        links.append(u)
        if len(links) >= limit:
            break
    return links


def _raw_html_links(r) -> str:
    """fetch_article 只回正文，不回原始 HTML；这里退而用正文里的裸链接。

    真实站点里正文常带 <a href>（vault_fetch 会保留一部分），够用；
    不够时由用户直接贴链接列表，不做过度工程。
    """
    return r.text or ""


# ---------------------------------------------------------------------------
# 2. 一条链接 → 一条库条目
# ---------------------------------------------------------------------------
def _item_id(url: str) -> str:
    return "u-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def make_opts(**kw):
    """给 HTTP 接口复用：一个不依赖 argparse 的选项对象。"""
    class _O:
        timeout = 20.0
        max_chars = 6000
        min_chars = 200
        min_confidence = 0.6
        no_model = False
    o = _O()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def build_item(url: str, backend, a, prefetched: dict | None = None) -> dict:
    """抓取 + 判立场 + 概括 + 归档。任何一步失败都留下原因，不抛异常。

    `prefetched` 是采集层提前拿到的内容（列表页上的标题 / 摘要）。
    为什么需要它：抖音、微博这类详情页要登录才看得见，抓正文经常失败；
    有列表页给的片段就先当正文用，至少标题和省流不会整条丢掉。
    片段够长（>= min_chars）就不再抓，省一次请求；够短就还是试一次正文。
    """
    base = {"id": _item_id(url), "url": url, "fetched_at": _now()}
    pre = prefetched or {}
    pre_text = (pre.get("text") or "").strip()
    pre_title = (pre.get("title") or "").strip()

    text = ""
    title = pre_title
    if len(pre_text) >= a.min_chars:
        text = pre_text
        base["fetch_method"] = "list_page"
    else:
        r = fetch_article(url, timeout=a.timeout)
        if r.ok:
            text = (r.text or "").strip()
            title = (r.title or "").strip() or pre_title
            base["fetch_method"] = r.method
        else:
            base["fetch_error"] = r.error
            if pre_text:
                text = pre_text
                base["fetch_method"] = "list_page(fallback)"

    if not text:
        base.update({
            "title": pre_title, "kind": "unreachable", "antipode_eligible": False,
            "error": base.get("fetch_error") or "取不到正文",
            "summary": "", "summary_from": "", "full_text": "", "text_chars": 0,
        })
        return base

    base.update({
        "title": title,
        "source": guess_source(url),
        "text_chars": len(text),
        "full_text": text[: a.max_chars],
        "error": "",
    })

    if len(text) < a.min_chars:
        base.update({"kind": "too_short", "antipode_eligible": False,
                     "summary": extractive_summary(text) or pre_text[:90],
                     "summary_from": "extractive" if extractive_summary(text) else "list_page"})
        return base

    # --- 判议题与立场（长文分块，避免态度卡在分片边界上）---
    # --no-model 时整段跳过：真零成本。此时不预判"是否值得找反面"，
    # 交给反引擎在跑的时候现场判（它会判立场、也会做观点性复检）。
    stance = None
    if not a.no_model:
        try:
            stances = extract_long(text) if len(text) > 3000 else extract(text)
            # 取置信度最高、且确实站了队的那条
            ranked = [s for s in stances if s.side in ("a", "b")]
            stance = max(ranked, key=lambda s: s.confidence) if ranked else (stances[0] if stances else None)
        except Exception as e:                                 # noqa: BLE001
            base["stance_error"] = f"{type(e).__name__}: {str(e)[:120]}"

    if stance is not None:
        base.update({
            "issue_id": stance.issue_id if stance.side in ("a", "b") else "",
            "supports": stance.supports if stance.side in ("a", "b") else "",
            "stance_side": stance.side,
            "stance_confidence": round(float(stance.confidence or 0), 3),
            "evidence_span": (stance.evidence_span or "")[:300],
            "verified": bool(stance.verified),
        })

    # --- 是不是"在表达主张"：只有这类才值得找对立面 ---
    has_stance = stance is not None and stance.side in ("a", "b") and stance.confidence >= a.min_confidence
    tutorialish = bool(TUTORIAL_HINT.search(title)) or is_tutorial_title(title)

    # 关键一问：有立场 ≠ 在论战。
    # 《深入了解微服务架构：原理、优势与实践指南》会被判成"支持微服务→0.95"，
    # 但它是在**介绍技术**，不是在跟"单体派"争论。给这种文章找反面，等于杠一个教程。
    # 所以对"有立场"的条目再问一次（另起一问，不动立场判据）。
    opinion = True
    if has_stance and not a.no_model:
        verdict = is_opinion_post(text, backend)
        if verdict is False:
            opinion = False
            base["opinion_check"] = "技术介绍（非主张）"

    if a.no_model:
        kind = "unclassified"                  # 没调模型，不预判；反引擎会自己判
    elif has_stance and opinion:
        kind = "opinion"
    elif tutorialish or (has_stance and not opinion):
        kind = "tutorial"
    elif stance is not None and stance.side in ("neutral",):
        kind = "neutral"
    else:
        kind = "reference"

    # ★ 议题依据：判出的议题必须在原文里真的有依据，否则这个 ★ 就是假的。
    # 实测：一批中文时事（票房预测、博物馆闭馆、农民工欠薪）被模型硬挑成
    # 「豆腐脑甜咸」「monorepo vs multirepo」——16 个议题一个都不沾边。
    # 这种条目即使在库里标了 ★，反库也只能凭空生成。
    #
    # 两支护栏：
    #   ① 判到梗/生活偏好类议题（豆腐脑、Tab vs 空格）→ 一律不标 ★。
    #      它们的反信息是人工预置的演示素材，不存在"检索出来"这回事。
    #   ② 判出的议题本身命中 0 次，且没有任何议题命中 ≥2 → 不标 ★。
    #      阈值取 2 而不是 1：单次命中常常是巧合（"company" 里的 "any"），
    #      而 0 次意味着连巧合都没有 —— 那才是真的没依据。
    if kind == "opinion" and stance is not None and stance.issue_id:
        cov = topic_coverage(text)
        zero_hit = cov.get(stance.issue_id, 0) == 0
        weak = (not cov) or max(cov.values(), default=0) < 2
        if stance.issue_id in NO_RETRIEVAL or (zero_hit and weak):
            kind = "reference"
            base["eligibility_reason"] = (
                f"判出的议题「{stance.issue_id}」在原文里没有依据"
                f"（命中 {cov.get(stance.issue_id, 0)} 次，全表最高 "
                f"{max(cov.values(), default=0)} 次）—— 不标 ★，不硬凑")
            # 把这条站不住的立场从条目上摘掉：留着它，下游会当既成事实用。
            # 原始判据留在 stance_dropped 里，答辩时能说明"模型判了，规则拦下了"。
            base["stance_dropped"] = {
                "issue_id": base.pop("issue_id", ""),
                "supports": base.pop("supports", ""),
                "stance_side": base.pop("stance_side", ""),
                "stance_confidence": base.pop("stance_confidence", 0),
                "evidence_span": base.pop("evidence_span", ""),
                "reason": "议题在原文无依据（关键词命中 0）",
            }

    base.update({
        "kind": kind,
        # None = 未判定（--no-model），交给下游；False = 明确不该找反面
        "antipode_eligible": None if a.no_model else (kind == "opinion"),
    })

    # --- 一句话省流（判立场已用全文，这里只为人读）---
    if a.no_model:
        base.update({"summary": extractive_summary(text, 80), "summary_from": "extractive"})
    else:
        s, src = make_summary(text, backend)
        base.update({"summary": s, "summary_from": src})

    return base


# ---------------------------------------------------------------------------
# 3. 写库
# ---------------------------------------------------------------------------
def write_vault(vault: dict, items: list[dict], out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    vpath = os.path.join(out_dir, f"vault_{vault['vault_id']}.json")
    ipath = os.path.join(out_dir, f"vault_{vault['vault_id']}.items.jsonl")

    with open(vpath, "w", encoding="utf-8") as fh:
        json.dump(vault, fh, ensure_ascii=False, indent=2)
    with open(ipath, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    return vpath, ipath


def load_existing(path: str) -> dict[str, dict]:
    """断点续跑：已抓过的按 url 归一下，不重复花钱。"""
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[_norm_url(d.get("url", ""))] = d
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="客户给的链接 → 一个仓库（抓正文 / 判立场 / 写省流）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python code/build_vault.py --urls links.txt --vault-id client-a --topic 前端选型",
    )
    ap.add_argument("--url", action="append", help="一个链接（可重复）")
    ap.add_argument("--urls", help="链接清单文件：一行一个，或一整段里夹着链接")
    ap.add_argument("--text", help="直接给一段话，自动抽出里面的链接")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读链接")
    ap.add_argument("--discover", action="store_true",
                    help="把 --url/--urls 里的页面当列表页，从里面发现文章链接")
    ap.add_argument("--vault-id", default="", help="库 ID（默认按日期生成）")
    ap.add_argument("--topic", default="", help="这个库是什么主题（给人看的）")
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "data"))
    ap.add_argument("--limit", type=int, default=0, help="最多处理几条")
    ap.add_argument("--max-chars", type=int, default=6000, help="每条存多少正文（判立场够用）")
    ap.add_argument("--min-chars", type=int, default=200, help="少于这么多字视为没正文")
    ap.add_argument("--min-confidence", type=float, default=0.6, help="判成观点类的最低置信度")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--no-model", action="store_true", help="不调模型：只抓取 + 抽取式摘要，零成本")
    ap.add_argument("--no-resume", action="store_true", help="不跳过已抓过的条目")
    ap.add_argument("--antipode", choices=["on", "off"], default="on",
                    help="这个库要不要开反推荐（邮件/代码类应设 off）")
    a = ap.parse_args(argv)

    urls = collect_urls(a)
    if not urls:
        print("❌ 没给链接。用 --url / --urls / --text / --stdin 之一。", file=sys.stderr)
        return 2

    if a.discover:
        pages = dedupe(urls)
        print(f"从一个列表页里发现链接：{pages[0] if pages else '—'}")
        found: list[str] = []
        for p in pages:
            found.extend(discover_links(p, limit=a.limit or 20))
        urls = found or urls
        print(f"  发现 {len(urls)} 条")

    urls = dedupe(urls)
    if a.limit:
        urls = urls[: a.limit]

    vault_id = a.vault_id or f"vault-{datetime.now(CST).strftime('%m%d-%H%M')}"
    ipath = os.path.join(a.out_dir, f"vault_{vault_id}.items.jsonl")
    old = {} if a.no_resume else load_existing(ipath)

    backend = None if a.no_model else OpenAICompatBackend()

    print("=" * 78)
    print(f"建库   库 {vault_id}   主题 {a.topic or '(未标)'}   链接 {len(urls)} 条"
          + (f"   已有 {len(old)} 条（续跑）" if old else ""))
    print("=" * 78)

    items: list[dict] = []
    skipped = 0
    for n, url in enumerate(urls, 1):
        key = _norm_url(url)
        if key in old and old[key].get("full_text"):
            items.append(old[key])
            skipped += 1
            print(f"  ⏭ {n:>2}. {url[:64]}   已有，跳过")
            continue

        t0 = time.time()
        it = build_item(url, backend, a)
        items.append(it)
        dt = time.time() - t0

        if it.get("error"):
            print(f"  ✕ {n:>2}. {url[:64]}\n       {it['error']}")
        else:
            flag = "★" if it["antipode_eligible"] else " "
            issue = it.get("issue_id") or it.get("kind", "")
            print(f"  {flag} {n:>2}. {it.get('title', '')[:34]:<36}"
                  f" {it.get('text_chars', 0):>5}字  {issue:<28} {dt:>4.1f}s")

    # 统计
    ok_items = [i for i in items if not i.get("error")]
    eligible = [i for i in items if i.get("antipode_eligible")]
    kinds: dict[str, int] = {}
    for i in items:
        kinds[i.get("kind", "?")] = kinds.get(i.get("kind", "?"), 0) + 1

    vault = {
        "vault_id": vault_id,
        "topic": a.topic,
        "antipode_enabled": a.antipode == "on",
        "created_at": _now(),
        "built_by": "build_vault.py",
        "source": "client-links",
        "item_count": len(items),
        "antipode_eligible_count": len(eligible),
        "kinds": kinds,
    }
    vpath, ipath = write_vault(vault, items, a.out_dir)

    print("-" * 78)
    print(f"  入库 {len(ok_items)} / {len(items)} 条"
          + (f"（失败 {len(items) - len(ok_items)}）" if len(items) > len(ok_items) else "")
          + (f"   续跑跳过 {skipped}" if skipped else ""))
    print("  类型：" + "  ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))
    if a.no_model:
        print("  ★ 可参与反推荐：未判定（--no-model 没调模型，留给反引擎跑的时候现场判）")
    else:
        print(f"  ★ 可参与反推荐：{len(eligible)} 条"
              "（只有「在表达主张」的才值得找对立面；教程/文档/新闻硬找只会得到抬杠）")
    print()
    print(f"  → {os.path.relpath(vpath, _ROOT)}")
    print(f"  → {os.path.relpath(ipath, _ROOT)}")
    print()
    print("  下一步：")
    print(f"    python code/run_vault.py --vault {os.path.relpath(vpath, _ROOT)} --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
