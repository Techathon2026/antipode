#!/usr/bin/env python3
"""GitHub 新项目日报：搜「最近新建的项目」→ 读 README → 中文省流 → 库 + 日报。

为什么走这条路
--------------
抖音那类短视频**天生没有对立面**：几十个字的文案撑不起论证，
判立场判不出东西，反推荐也没处落。GitHub 项目同理 —— 代码仓库没有"观点"。
所以这两个源只用做**信息收集**：把「最近冒出来的东西」读一遍、压成中文省流、
附上原文地址。反推荐只留给知乎那种有长篇观点的源。

数据从哪来
----------
`GET /search/repositories?q=created:>=<日期>`：GitHub 官方搜索，**不需要登录**，
按 star 排序就是"最近大家都在看什么"。README 走 `/repos/{full}/readme`，
`Accept: application/vnd.github.raw` 直接拿 Markdown 原文（不解 base64）。

token 的取舍
------------
没有 token 也能跑（每小时 60 次）；有 token 是 5000 次/小时。
所以 token 是**加速项不是必需项**：拿不到就降级匿名跑，不阻断流程。

用法
----
    # 最近 7 天、star 最高的 10 个新项目
    python collect_github.py --days 7 --limit 10

    # 只看 Python / 只算有 50 star 以上的
    python collect_github.py --days 30 --lang python --min-stars 50

    # 不调模型（离线跑通链路，省流退化成首段）
    python collect_github.py --days 7 --limit 5 --no-model
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIRST = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

import summarize                                              # noqa: E402
from build_vault import _item_id, _now, write_vault           # noqa: E402

API = "https://api.github.com"
_UA = "antipode-digest"

# 不该出现在日报里的仓库。搜索是按 star 排的，而 star 涨得最快的项目里
# 总有一批成人内容 —— 不拦掉，日报第一屏就没法给人看了。
# 判断只扫「名字 + 描述 + topics」，不读 README：读正文太贵，而且这类项目
# 在元数据里就写得很直白。
_NSFW_WORDS = (
    "nsfw", "porn", "xxx", "hentai", "adult-only", "18+", "erotic",
    "色情", "成人视频", "裸", "福利姬",
)


def _is_nsfw(r: dict) -> bool:
    blob = " ".join([
        (r.get("name") or ""),
        (r.get("full_name") or ""),
        (r.get("description") or ""),
        " ".join(r.get("topics") or []),
    ]).lower()
    return any(w in blob for w in _NSFW_WORDS)


# ---------------------------------------------------------------------------
# 1. 取 token（拿不到就算了）
# ---------------------------------------------------------------------------
def _token(explicit: str = "") -> str:
    """按「显式传入 → 环境变量 → 本机 git 凭据」的顺序找 token。

    第三档是给"开发者自己机器上不用配任何东西"用的：GitHub 凭据通常已经
    存在凭据管理器里了，`git credential fill` 能取出来，不必再建一个 PAT。
    任何一步失败都静默降级 —— 匿名请求也能跑，只是每小时 60 次。
    """
    if explicit:
        return explicit.strip()
    for k in ("GITHUB_TOKEN", "GH_TOKEN", "ANTIPODE_GITHUB_TOKEN"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    try:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:                                          # noqa: BLE001
        pass
    return ""


# ---------------------------------------------------------------------------
# 2. 调 API
# ---------------------------------------------------------------------------
def _get(path: str, token: str, accept: str = "application/vnd.github+json",
         raw: bool = False, timeout: float = 30.0):
    url = path if path.startswith("http") else API + path
    headers = {"Accept": accept, "User-Agent": _UA,
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)

    last = ""
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return body.decode("utf-8", "replace") if raw \
                    else json.loads(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 403/429 基本都是限流：等一会儿再来，别把"限流"当"没这个项目"
            if e.code in (403, 429) and attempt < 3:
                time.sleep(2 * attempt)
                last = f"HTTP {e.code}（限流）"
                continue
            if e.code == 404:
                return None if not raw else ""
            last = f"HTTP {e.code}"
            break
        except Exception as e:                                 # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            if attempt < 3:
                time.sleep(1.5 * attempt)
                continue
            break
    raise RuntimeError(f"请求失败（{last}）：{url}")


def search_new(days: int, limit: int, min_stars: int = 0,
               lang: str = "") -> list[dict]:
    since = (dt.date.today() - dt.timedelta(days=max(1, days))).isoformat()
    q = f"created:>={since}"
    if min_stars > 0:
        q += f" stars:>={min_stars}"
    if lang:
        q += f" language:{lang}"
    # 多取一些：有些项目没有 README、有些会被后面的过滤丢掉，留余量
    per = min(100, max(limit * 2, 10))
    path = ("/search/repositories?" + urllib.parse.urlencode({
        "q": q, "sort": "stars", "order": "desc", "per_page": per,
    }))
    data = _get(path, _token()) or {}
    # 不在这里截 limit：后面还要过 NSFW 过滤，先截就会凑不满
    return (data.get("items") or [])


def fetch_readme(full_name: str, token: str) -> str:
    """拿 README 的 Markdown 原文。没有 README 返回空串，不算错误。"""
    try:
        got = _get(f"/repos/{full_name}/readme", token,
                   accept="application/vnd.github.raw", raw=True)
        return got or ""
    except RuntimeError:
        return ""


# ---------------------------------------------------------------------------
# 3. 仓库 → 条目
# ---------------------------------------------------------------------------
def repo_to_item(r: dict, readme: str, s: dict) -> dict:
    full = r.get("full_name") or r.get("name") or ""
    desc = (r.get("description") or "").strip()
    lang = r.get("language") or ""
    stars = int(r.get("stargazers_count") or 0)
    forks = int(r.get("forks_count") or 0)
    topics = [t for t in (r.get("topics") or []) if t][:8]

    # 标题保留 owner/repo，因为这是人点进 GitHub 最先认的东西；
    # 中文意思放进 summary 第一句，不塞进标题里挤掉原名。
    title = full or desc[:60]
    meta = " · ".join(x for x in (lang, f"{stars} star") if x)

    return {
        "id": _item_id(r.get("html_url", "")),
        "source": "github",
        "source_type": "repo",
        "url": r.get("html_url", ""),
        "title": title,
        "summary": s.get("summary", ""),
        "points": s.get("points", []),
        "summary_from": s.get("summary_from", ""),
        "description_en": desc,
        "full_text": summarize.clip(summarize.clean_markdown(readme), 4000),
        "readme_chars": len(readme or ""),
        "has_readme": bool(readme),
        "stars": stars,
        "forks": forks,
        "language": lang,
        "topics": topics,
        "created_at": r.get("created_at", ""),
        "pushed_at": r.get("pushed_at", ""),
        "meta": meta,
        "kind": "repo",
        # 代码仓库没有"对立面"，这条硬规则不因为项目多热而改变
        "antipode_eligible": False,
        "antipode_reason": "代码仓库没有观点立场，不参与反推荐",
        "fetched_at": _now(),
    }


# ---------------------------------------------------------------------------
# 4. 给人看的日报
# ---------------------------------------------------------------------------
def render_digest(vault: dict, items: list[dict], days: int) -> str:
    lines = [
        f"# {vault['topic']}",
        "",
        f"> 最近 {days} 天新建的项目，按 star 排序，共 {len(items)} 个。"
        "每条是中文省流 + 原文地址，点最后一行可直达。",
        "",
        f"生成时间：{vault['created_at']}",
        "",
        "---",
        "",
    ]
    for n, it in enumerate(items, 1):
        lines.append(f"## {n}. {it['title']}")
        lines.append("")
        if it.get("meta"):
            lines.append(f"`{it['meta']}`" + (f" · 创建于 {it['created_at'][:10]}"
                                              if it.get("created_at") else ""))
            lines.append("")
        if it.get("summary"):
            lines.append(f"**省流**：{it['summary']}")
            lines.append("")
        pts = it.get("points") or []
        if pts:
            lines.append("**要点**")
            for p in pts:
                lines.append(f"- {p}")
            lines.append("")
        if not it.get("has_readme"):
            lines.append("_（这个项目还没写 README，省流基于仓库描述）_")
            lines.append("")
        if it.get("topics"):
            lines.append("标签：" + " ".join(f"`{t}`" for t in it["topics"]))
            lines.append("")
        lines.append(f"原文：{it['url']}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="GitHub 新项目 → README 省流 → 库 + 日报")
    ap.add_argument("--days", type=int, default=7, help="最近几天创建（默认 7）")
    ap.add_argument("--limit", type=int, default=10, help="最多几个项目（默认 10）")
    ap.add_argument("--min-stars", type=int, default=0, help="最低 star 数")
    ap.add_argument("--lang", default="", help="只看某种语言，如 python")
    ap.add_argument("--token", default="", help="GitHub token（不给就自动找）")
    ap.add_argument("--topic", default="", help="库的标题")
    ap.add_argument("--vault-id", default="", help="库 id（默认按日期生成）")
    ap.add_argument("--no-model", action="store_true",
                    help="不调模型，省流退化成正文首段")
    ap.add_argument("--max-chars", type=int, default=7000,
                    help="喂给模型的 README 字数上限")
    ap.add_argument("--out-dir", default=os.path.join(_FIRST, "data"))
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="每个项目之间停一下，别把搜索接口打太快")
    a = ap.parse_args(argv)

    token = _token(a.token)
    print(f"\n  GitHub 新项目日报   最近 {a.days} 天   "
          f"{'已认证' if token else '匿名（每小时 60 次）'}")

    print(f"  [1/3] 搜索新项目 …", flush=True)
    try:
        repos = search_new(a.days, a.limit, a.min_stars, a.lang)
    except Exception as e:                                     # noqa: BLE001
        print(f"  [x] 搜索失败：{e}", file=sys.stderr)
        return 1
    if not repos:
        print("  [x] 这几天没有符合条件的项目，把 --days 放大试试。")
        return 1
    before = len(repos)
    repos = [r for r in repos if not _is_nsfw(r)][:a.limit]
    if len(repos) < before:
        print(f"        跳过 {before - len(repos)} 个不适合展示的仓库（成人内容）")
    print(f"        找到 {len(repos)} 个，开始读 README")

    items: list[dict] = []
    for n, r in enumerate(repos, 1):
        full = r.get("full_name", "?")
        readme = fetch_readme(full, token)
        # 没有 README 时用仓库描述当材料，至少给出"这是什么"
        material = readme or (r.get("description") or "")
        s = ({"summary": (r.get("description") or full)[:160],
              "points": [], "summary_from": "description"}
             if a.no_model else
             summarize.summarize(full, material,
                                 extra=(r.get("description") or ""),
                                 max_chars=a.max_chars))
        it = repo_to_item(r, readme, s)
        items.append(it)
        stars = it["stars"]
        print(f"  {n:>2}. {full[:38]:<40} {stars:>5}★ "
              f"{it['readme_chars']:>6}字  {it['summary'][:30]}")
        if a.sleep:
            time.sleep(a.sleep)

    vault_id = a.vault_id or f"github-new-{_now()[5:16].replace(':', '').replace('-', '')}"
    vault = {
        "vault_id": vault_id,
        "topic": a.topic or f"GitHub 最近 {a.days} 天的新项目",
        "antipode_enabled": False,          # 代码仓库不做反推荐
        "counter_types": [],
        "created_at": _now(),
        "built_by": "collect_github.py",
        "source": "github:new-repos",
        "window_days": a.days,
        "item_count": len(items),
        "antipode_eligible_count": 0,
        "query": {"days": a.days, "min_stars": a.min_stars, "lang": a.lang},
    }
    vpath, ipath = write_vault(vault, items, a.out_dir)
    mpath = os.path.join(a.out_dir, f"digest_{vault_id}.md")
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(render_digest(vault, items, a.days))

    print()
    print("  " + "-" * 66)
    print(f"  入库 {len(items)} 条（无反推荐 —— 代码仓库没有对立面）")
    print(f"  → {os.path.relpath(vpath, _FIRST)}")
    print(f"  → {os.path.relpath(mpath, _FIRST)}   ← 给人看的日报")
    print()
    print("  想看某一条的原文，日报里每段最后一行就是链接。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
