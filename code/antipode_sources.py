#!/usr/bin/env python3
"""反引擎检索适配器 —— 从公开源里捞"与库条目相反"的内容候选。

【这个模块只做一件事：检索】
它不判断立场、不打分、不摘要。那些是 stance_extractor 与 engine 的事。
职责分离的原因：检索可以离线测、可以换源、可以反复调参，不该和判断逻辑缠在一起。

支持的源：
    v2ex    中文 · V2EX 讨论帖（经 sov2ex 第三方搜索接口，官方 API 没有搜索）
            · 中文第一手经验与吐槽密度最高，"我们公司上了微服务结果……"这类内容极多
            · 帖子自带回复数，是免费的质量信号
    juejin  中文 · 掘金文章搜索（官方 search_api）
            · 偏教程与知识总结，观点密度低于 V2EX，作为补充源
            · ⚠️ 两个坑：① 忽略 limit 参数，必须自己截断；② 常用词如「单体架构」可能返回 0 条
    hn      英文 · Hacker News（Algolia API）
            · 无需鉴权，文章标题即观点，可用 URL 直通原文博客
    github  GitHub 搜索 API
            · ⚠️ 实测不适合找观点（见 docs/反仓库检索_实测记录.md），保留作定向查询用
            · 有 token：30 次/分钟；无 token：10 次/分钟

用法：
    # 1) 离线自检：检索词表是否与议题清单对齐（不联网）
    python antipode_sources.py --selftest

    # 2) 可行性探针：库条目支持「微服务」，去找支持「单体」的内容
    python antipode_sources.py --probe microservices_vs_monolith --supports 微服务

    # 3) 只看中文源 / 只看英文源
    python antipode_sources.py --probe rust_vs_cpp --supports Rust --sources v2ex,juejin
    python antipode_sources.py --probe rust_vs_cpp --supports Rust --sources hn

    # 4) 调参：直接跑一个原始 query，不查词表
    python antipode_sources.py --raw-query 过度设计 --sources v2ex

设计约束：
    · 零第三方依赖（只用标准库），保证在任何机器上都能直接跑
    · 议题清单与对立面推导**复用 stance_extractor**，不另起一套
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field, replace

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from stance_extractor import ISSUE_TAXONOMY, opposite_side  # noqa: E402
from retrieval_terms import ISSUE_TERMS, TERMS_VERSION, NO_RETRIEVAL  # noqa: E402

USER_AGENT = "antipode-hackathon/0.1"
HTTP_TIMEOUT = 25

# 所有已实现的源
SOURCES = ("v2ex", "juejin", "hn", "github")

# 中文源 / 英文源分组。
#
# ★ 2026-10-16 晚调整：用户指出**本产品面向国内用户，HN 在国内基本没有市场**。
#   所以源策略从"中文优先 + 英文补充"改成**中文为唯一默认**：
#     · 一个给国内用户用的产品，反库里塞满英文硅谷博客是产品错位，不是语言偏好问题
#     · HN 保留为**可选源**：只在源内容本身是英文时才该启用（`--sources hn`）
#   实测依据：V2EX 的 `_source.content` 就是帖子完整正文（500+ 字），
#   足够支撑证据与摘要；反观 HN 的文章模式只有标题，反而更薄。
CN_SOURCES = ("v2ex", "juejin")
EN_SOURCES = ("hn",)

# 检索"对立观点"时的默认源：**只用中文源**（理由见上）。
# ⚠️ GitHub 也不在默认里 —— 实测它不适合做观点检索源（搜 monolith 返回的是
# 33KB 内核模块化草稿、webpack 构建优化、图片下载进度条）。
# GitHub 的价值在**收集侧**（队友读用户 star 的仓库 / issue），不在反引擎这一侧。
RETRIEVAL_SOURCES = CN_SOURCES


# ---------------------------------------------------------------------------
# 1. 统一候选结构 —— 所有源都吐这个，后面的判断逻辑不需要知道数据从哪来
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    source: str
    candidate_id: str
    url: str
    title: str
    text: str
    query: str = ""
    created_at: str = ""
    popularity: int = 0
    # 命中来自哪一档（对应下面的三档检索策略）：origin / global / synthesized
    stage: str = ""
    extra: dict = field(default_factory=dict)

    def digest(self) -> dict:
        d = asdict(self)
        d["text_len"] = len(self.text)
        return d


class RateLimited(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 2. HTTP 小工具（stdlib）
# ---------------------------------------------------------------------------
def _http_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if e.code in (403, 429):
            raise RateLimited(f"HTTP {e.code}（疑似触发限流）：{body}") from e
        raise RuntimeError(f"HTTP {e.code}：{body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络不可达：{e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"返回不是合法 JSON：{e}") from e


def strip_html(s: str) -> str:
    """HN 的 comment_text 是 HTML 片段，剥成纯文本"""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _load_env_value(name: str) -> str:
    """先看环境变量，再兜底读 code/.env（与 stance_extractor 的密钥约定一致）"""
    if os.environ.get(name):
        return os.environ[name].strip()
    env_path = os.path.join(_HERE, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == name:
                        return v.strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


# ---------------------------------------------------------------------------
# 3. 源适配器
# ---------------------------------------------------------------------------
def search_github(query: str, limit: int, token: str = "", scope: str = "body") -> list[Candidate]:
    """GitHub issue / PR 搜索。

    scope 决定匹配范围：
        body   加 in:body —— 匹配正文
        title  加 in:title —— 只匹配标题
        raw    原样发出去，不追加任何限定符（调参时用）

    ⚠️ 曾经的坑：无条件追加 in:body，导致调用方传 "xxx in:title" 时变成
    "xxx in:title in:body"，限定符互相干扰、结果完全不对。所以 scope=raw 是必需的。
    """
    if scope == "raw" or " in:" in query:
        q = query
    else:
        q = f"{query} in:{scope}"

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url_sorted = (
        "https://api.github.com/search/issues?"
        + urllib.parse.urlencode({"q": q, "per_page": limit, "sort": "reactions", "order": "desc"})
    )
    try:
        data = _http_json(url_sorted, headers)
    except RuntimeError:
        url_plain = (
            "https://api.github.com/search/issues?"
            + urllib.parse.urlencode({"q": q, "per_page": limit})
        )
        data = _http_json(url_plain, headers)

    out: list[Candidate] = []
    for it in data.get("items", []):
        url = it.get("html_url") or ""
        if not url:
            continue
        repo = ""
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/", url)
        if m:
            repo = m.group(1)
        num = it.get("number")
        body = it.get("body") or ""
        out.append(
            Candidate(
                source="github",
                candidate_id=f"{repo}#{num}" if repo else str(it.get("id")),
                url=url,
                title=it.get("title") or "",
                text=f"{it.get('title') or ''}\n\n{body}".strip(),
                query=query,
                created_at=it.get("created_at") or "",
                popularity=int((it.get("reactions") or {}).get("total_count") or 0),
                extra={"comments": it.get("comments"), "state": it.get("state")},
            )
        )
    return out


def search_hn(query: str, limit: int, tags: str = "comment") -> list[Candidate]:
    """Hacker News 搜索（Algolia API）。

    tags 的选择很关键，两者用途不同：
      comment  评论区 —— "我踩了这个坑"类的第一手经验，反向证据的主要来源
      story    文章标题 —— 标题本身就是观点（"We moved back to a monolith"），
               适合用来找**论点载体**，再顺着 URL 回现场读原文

    注意：Algolia 默认是**宽松匹配**，多词查询会被拆开并倾向于返回高热度内容
    （招聘帖、热榜汇总会污染结果）。关键短语请加引号做精确匹配。
    """
    url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
        {"query": query, "tags": tags, "hitsPerPage": limit}
    )
    data = _http_json(url)

    out: list[Candidate] = []
    for it in data.get("hits", []):
        if tags == "comment":
            text = strip_html(it.get("comment_text") or "")
            title = it.get("story_title") or ""
        else:
            text = strip_html(it.get("story_text") or "") or (it.get("title") or "")
            title = it.get("title") or ""
        if not text:
            continue
        oid = it.get("objectID")
        out.append(
            Candidate(
                source="hn",
                candidate_id=f"hn-{oid}",
                url=it.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                title=title,
                text=(f"{title}\n\n{text}" if tags != "comment" else text).strip(),
                query=query,
                created_at=it.get("created_at") or "",
                popularity=int(it.get("points") or 0),
                extra={"author": it.get("author"), "hn_item": f"https://news.ycombinator.com/item?id={oid}", "tags": tags},
            )
        )
    return out


def search_v2ex(query: str, limit: int, sort: str = "") -> list[Candidate]:
    """中文源主力：V2EX 讨论帖（经 sov2ex 搜索接口）。

    为什么用它：V2EX 是中文开发者**表达观点和吐槽**的地方，
    "我们公司上了微服务结果……"这种第一手经验密度极高，
    而帖子自带回复数 —— 相当于 HN 的 points，是免费的质量信号。

    ⚠️ 查询必须是**短词**（最好 1 个词）。实测搜「微服务 后悔」会退化成松散 OR，
    捞回"后悔做产品经理""结婚之后是男方后悔多"这类完全无关的帖子。

    ⚠️ sov2ex 是第三方服务，不是 V2EX 官方（官方 API 没有搜索接口）。
    答辩时若被问到，要如实说明。
    """
    params = {"q": query, "size": limit}
    if sort:
        params["sort"] = sort
    url = "https://www.sov2ex.com/api/search?" + urllib.parse.urlencode(params)
    data = _http_json(url)

    out: list[Candidate] = []
    for h in data.get("hits") or []:
        src = h.get("_source") or {}
        hl = h.get("highlight") or {}
        title = (src.get("title") or "").strip()

        # ★ 关键：`_source.content` 是**帖子完整正文**，而 highlight 只是命中的那一小段。
        #   第一版误用了 highlight（117 字），把 571 字的正文浪费了 —— 结果就是
        #   候选"内容太薄"，模型判立场只能给 0.6 的置信度。
        #   这里改成优先用完整正文，highlight 只在正文缺失时兜底。
        body = re.sub(r"\s+", " ", (src.get("content") or "")).strip()
        snippet = " ".join(hl.get("content") or [])
        snippet = html.unescape(re.sub(r"</?em>", "", snippet))
        if not body:
            body = re.sub(r"\s+", " ", snippet).strip()

        tid = h.get("_id")
        out.append(
            Candidate(
                source="v2ex",
                candidate_id=f"v2ex-{tid}",
                url=f"https://www.v2ex.com/t/{tid}",
                title=title,
                text=f"{title}\n\n{body}".strip(),
                query=query,
                created_at=(src.get("created") or "")[:19],
                popularity=int(src.get("replies") or 0),
                extra={
                    "replies": src.get("replies"),
                    "node": src.get("node"),
                    "highlight": snippet[:200],
                    "content_len": len(body),
                },
            )
        )
    return out


def fetch_hn_story_comments(story_id: str, limit: int = 6, min_len: int = 60) -> list[Candidate]:
    """取某篇 HN 文章下的讨论评论。

    为什么需要它：HN 的**文章模式只有标题**（"Monoliths Are the Future"），
    拿它当证据等于把"声明"当"论据"。而讨论区里全是真论证 ——
    实测某篇文章下 445 条评论，随手一条就是 500 字的成本对比。

    Algolia 支持 `tags=comment,story_<id>` 按文章取评论，纯 JSON，无需抓网页。
    每条评论都有永久链接（`news.ycombinator.com/item?id=<objectID>`），
    所以**证据本身可以点开验证** —— 这对"回现场"铁律是加分项。
    """
    url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
        {"tags": f"comment,story_{story_id}", "hitsPerPage": max(limit * 4, 16)}
    )
    data = _http_json(url)

    out: list[Candidate] = []
    for it in data.get("hits", []):
        text = strip_html(it.get("comment_text") or "")
        if len(text) < min_len:
            continue
        oid = it.get("objectID")
        out.append(
            Candidate(
                source="hn",
                candidate_id=f"hn-{oid}",
                url=f"https://news.ycombinator.com/item?id={oid}",
                title=it.get("story_title") or "",
                text=text,
                query="(story comments)",
                created_at=it.get("created_at") or "",
                popularity=int(it.get("points") or 0),
                stage="enrich",
                extra={"author": it.get("author"), "tags": "comment", "story_id": story_id},
            )
        )
    # 优先有论证痕迹、其次更长的
    out.sort(key=lambda c: (looks_argumentative(c.text), len(c.text)), reverse=True)
    return out[:limit]


def enrich_candidate(c: Candidate, comment_limit: int = 3, max_chars: int = 1400) -> Candidate:
    """给"只有标题"的候选补论证素材。

    只处理 HN 文章模式的候选；补进来的评论会存进 `extra["comments"]`，
    由引擎**逐条判立场**，挑出真正站对立面的那一条当证据 ——
    而不是把一整段混合正反观点的讨论当成一个整体去判（讨论区天然是两派在吵）。
    """
    if c.source != "hn" or (c.extra or {}).get("tags") != "story":
        return c
    if len(c.text) >= 200:  # 已经有正文了
        return c

    story_id = str((c.extra or {}).get("story_id") or c.candidate_id.replace("hn-", ""))
    if not story_id.isdigit():
        return c

    try:
        comments = fetch_hn_story_comments(story_id, limit=comment_limit)
    except Exception as e:
        print(f"    [!] 补评论失败（story {story_id}）：{e}", file=sys.stderr)
        return c
    if not comments:
        return c

    parts = [c.title.strip()]
    total = len(c.title)
    used = []
    for cm in comments:
        if total + len(cm.text) > max_chars:
            break
        parts.append(cm.text)
        total += len(cm.text)
        used.append({"text": cm.text, "url": cm.url, "author": (cm.extra or {}).get("author")})

    return replace(
        c,
        text="\n\n".join(parts),
        extra={
            **(c.extra or {}),
            "enriched": True,
            "comments_used": len(used),
            "comments": used,
            "discussion_url": f"https://news.ycombinator.com/item?id={story_id}",
        },
    )


def search_juejin(query: str, limit: int) -> list[Candidate]:
    """中文补充源：掘金文章搜索（官方 search_api）。

    定位说明：掘金偏教程与知识总结（"微服务设计原则""六种常用设计模式"），
    **观点密度明显低于 V2EX**。它的价值在于偶尔能捞到复盘型文章，
    例如"微服务架构过度设计的思考与实践"。所以排在 V2EX 之后作为补充。

    ⚠️ 两个已实测的坑：
      1. 接口**忽略 limit 参数**（要 5 条会返回 20 条）→ 必须自己截断
      2. 部分常用词返回 0 条（如「单体架构」），召回不如 V2EX 稳定
    """
    url = "https://api.juejin.cn/search_api/v1/search?aid=2608&uuid=0&spider=0"
    payload = json.dumps(
        {
            "key_word": query,
            "id_type": 2,
            "cursor": "0",
            "limit": str(limit),
            "search_type": 0,
            "sort_type": 0,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Origin": "https://juejin.cn",
            "Referer": "https://juejin.cn/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络不可达：{e.reason}") from e

    out: list[Candidate] = []
    for it in (data.get("data") or [])[:limit]:  # ← 必须截断，接口不认 limit
        model = it.get("result_model") or {}
        ai = model.get("article_info") or {}
        if not ai:
            continue
        aid = ai.get("article_id")
        title = (ai.get("title") or "").strip()
        brief = re.sub(r"\s+", " ", (ai.get("brief_content") or "")).strip()
        out.append(
            Candidate(
                source="juejin",
                candidate_id=f"juejin-{aid}",
                url=f"https://juejin.cn/post/{aid}",
                title=title,
                text=f"{title}\n\n{brief}".strip(),
                query=query,
                created_at="",
                popularity=int(ai.get("view_count") or 0),
                extra={
                    "user": (model.get("author_user_info") or {}).get("user_name"),
                    "digg": ai.get("digg_count"),
                },
            )
        )
    return out


ADAPTERS = {
    "github": search_github,
    "hn": search_hn,
    "v2ex": search_v2ex,
    "juejin": search_juejin,
}

# ---------------------------------------------------------------------------
# ★ 三档检索优先级（用户 2026-10-16 定的策略）
# ---------------------------------------------------------------------------
#   第一档 origin —— **原条目所在的那个源**。库条目来自 V2EX 就只在 V2EX 里找，
#                    来自 GitHub 就只在 GitHub 里找。这一档最聪明的地方在于：
#                    同一个社区里的人，讨论语境、术语、隐含前提是一致的，
#                    找出来的"对立面"才是**同一个世界里的对立面**，
#                    不会出现"中文帖子旁边配一篇硅谷博客"的割裂感。
#   第二档 global —— 全网搜（所有已实现的源），第一档不够用时才启用。
#   第三档 synthesized —— AI 自编。**默认关闭**，原因见下。
#
# ⚠️ 第三档的硬约束（必须遵守，否则整个产品的可信度根基会塌）：
#   1. **绝不能编 URL**。没有真实 URL 的条目，宁可不要。
#      反推荐的卖点是"真有另一面"，不是"AI 会抬杠"。
#   2. 必须打标记 `synthesized: true`，且界面上与真实来源**视觉区分**
#      （建议灰色虚线框 + 明确写"模型推测，非真实来源"）。
#   3. 默认关闭，要显式开启。
#   4. 答辩时必须主动交代哪几条是自编的。
#   → 更安全的做法：把第三档实现成"**人工预置真实语料**"——
#     提前从真实帖子里挑好存下来。它同样是兜底，但它不是编的。
ORIGIN_TO_SOURCE = {
    "v2ex": "v2ex",
    "juejin": "juejin",
    "hn": "hn",
    "hackernews": "hn",
    "github": "github",
}

SYNTHESIZED_ENABLED = False  # 第三档默认关闭，见上面的硬约束

# 招聘 / 广告类噪声。V2EX 上这类帖子占比很高，一条正则就能剔掉一大批。
# 实测：搜「坐班」时捞回的大多是"招聘：需驻场坐班开发""求活，可短时坐班可外包"，
# 跟观点毫无关系。放在结果侧过滤，比在查询里想办法更省事。
NOISE_PATTERNS = re.compile(
    r"(招聘|招人|内推|求职|求活|投简历|简历|岗位|外包|兼职|时薪|驻场|海投)",
)


# 汇总/周刊类噪声：这类帖子是"链接合集"，本身不表达观点。
# 实测：搜出来的 "DTO Weekly Curations #2" 被判成对立内容并进了反库 —— 但它只是
# 转载了别人的文章标题，标题里的立场不属于发帖人。汇总帖不是观点载体，剔除。
DIGEST_PATTERNS = re.compile(
    r"(周刊|周报|日报|合集|汇总|导航|盘点|第\s*\d+\s*期"
    r"|Weekly|Curation|Newsletter|Digest|Top\s*\d+)",
    re.I,
)


def is_noise(c: Candidate) -> bool:
    """自报"我是招聘/广告/汇总"的，直接丢掉。"""
    title = c.title or ""
    return bool(NOISE_PATTERNS.search(title) or DIGEST_PATTERNS.search(title))


def is_usable_candidate(c: Candidate, anchors: list[str]) -> bool:
    """粗判一条候选"值不值得送进模型"。只是**判断是否要补下一档**用的，
    最终判定权在 stance_extractor，这里宁可宽松。

    为什么按"可用数"而不是"候选数"决定要不要补全网：
    实测过 —— 原条目来自 V2EX 时，第一档捞回 13 条，看着很够，
    但里面混着"monolith 是个把网页打包成单文件的工具"这种同名假朋友，
    真正沾题带立场的只有 1 条。按候选数判断会误以为够了。
    """
    return keyword_hit(c.text, anchors) and has_stance_signal(c)


def _delay_for(source: str, has_github_token: bool) -> float:
    if source in ("hn", "v2ex"):
        return 0.5
    if source == "juejin":
        return 1.2
    return 2.2 if has_github_token else 6.5  # 30/min vs 10/min 的礼貌间隔


def collect(
    issue_id: str,
    want_side: str,
    limit: int = 5,
    sources: tuple[str, ...] = RETRIEVAL_SOURCES,
    tier: str = "both",
    hn_tags: str = "story",
    sleep: bool = True,
    origin: str = "",
    anchors: list[str] | None = None,
    min_usable: int = 3,
    allow_global: bool = True,
) -> list[Candidate]:
    """给定议题和"想要的一方"，按三档优先级检索，返回去重后的候选。

    检索顺序（用户 2026-10-16 定的策略）：
        origin  原条目所在的源 —— 先只在这里找（同一个社区的对立面才是"同一个世界的对立面"）
        global  全网 —— 第一档的**可用候选**不足 min_usable 条时才启用
        （第三档 synthesized 不在本函数内，它不属于"检索"，见文件头硬约束）

    ⚠️ 判断"够不够"用的是**可用候选数**（沾题 + 带立场信号），不是候选总数。
    实测过：按总数判断会误以为够了 —— 第一档捞回 13 条，其中 12 条是同名假朋友。

    tier 决定召回词：
        side   只用该方自己的词（精确召回，命中率高但条数少）
        topic  只用议题关键词（宽召回，两方内容都会进来，靠模型筛）
        both   两者都用（默认推荐：先精确、再补宽召回）

    每个候选都会带上 `stage` 字段，标明它是哪一档找来的 —— 这个字段会一路带到反库条目的
    `found_by.stage` 里，答辩时可以逐条说清"这条是怎么来的"。
    """
    entry = ISSUE_TERMS.get(issue_id)
    if entry is None:
        raise KeyError(f"术语表里没有议题 {issue_id}")
    if entry.get("retrieval") == "none":
        raise ValueError(f"{issue_id} 被标记为不做检索（人工预置演示）")

    terms = list(entry["sides"].get(want_side) or [])
    if tier in ("topic", "both"):
        extra = [t for t in entry["topics"] if t not in terms]
        terms = extra if tier == "topic" else terms + extra
    if not terms:
        raise KeyError(f"术语表里没有 {issue_id} / {want_side} 的检索词")

    token = _load_env_value("GITHUB_TOKEN")
    seen: set[str] = set()
    results: list[Candidate] = []
    dropped_noise = 0

    # 组装档位
    stages: list[tuple[str, tuple[str, ...]]] = []
    origin_src = ORIGIN_TO_SOURCE.get((origin or "").lower())
    if origin_src and origin_src not in sources:
        # 原条目来自一个当前没启用的源（比如只开了 hn，但条目来自 v2ex）→ 也给它加上
        sources = (origin_src,) + tuple(s for s in sources if s != origin_src)
    if origin_src:
        stages.append(("origin", (origin_src,)))
    if allow_global:
        stages.append(("global", tuple(sources)))
    if not stages:
        stages.append(("", tuple(sources)))

    for stage_name, srcs in stages:
        # 第一档已经攒够**可用**候选就不必全网扫了 —— 这正是"同源优先"的意义。
        # 注意算的是 usable 而不是 len(results)：候选多不等于有用（同名假朋友问题）。
        if stage_name == "global" and anchors is not None:
            usable_now = sum(1 for c in results if is_usable_candidate(c, anchors))
            if usable_now >= min_usable:
                print(f"    [优先级] 同源已给出 {usable_now} 条可用候选 → 不启用全网搜", file=sys.stderr)
                break
            print(f"    [优先级] 同源只有 {usable_now} 条可用候选 → 补全网搜", file=sys.stderr)
        elif stage_name == "global" and anchors is None and len(results) >= min_usable:
            break

        for term in terms:
            for source in srcs:
                fn = ADAPTERS[source]
                try:
                    if source == "github":
                        batch = fn(term, limit, token, "body")
                    elif source == "hn":
                        batch = fn(term, limit, hn_tags)
                    else:
                        batch = fn(term, limit)
                except RateLimited as e:
                    print(f"    [!] {source} 限流，跳过「{term}」：{e}", file=sys.stderr)
                    time.sleep(5)
                    continue
                except Exception as e:  # 单个 query 失败不该拖垮整轮
                    print(f"    [!] {source} 检索「{term}」失败：{e}", file=sys.stderr)
                    continue
                for c in batch:
                    if c.url in seen:
                        continue
                    seen.add(c.url)
                    if is_noise(c):
                        dropped_noise += 1
                        continue
                    c.stage = stage_name
                    results.append(c)
                if sleep:
                    time.sleep(_delay_for(source, bool(token)))

    if dropped_noise:
        print(f"    [过滤] 剔除 {dropped_noise} 条招聘/广告类噪声", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# 4. 可行性评估 —— 把"能不能用"变成可量化的数字
# ---------------------------------------------------------------------------
# 注意：这里是**探针**的粗指标，不是最终判据。真正的判断交给 stance_extractor。
# 为什么需要粗指标：如果不先量化，就会像第一轮那样被"22 条可用"骗过去 ——
# 实际上捞回来的是 33KB 的内核草稿。粗指标的作用是**尽早暴露"看起来很行其实不行"**。

# 有论证痕迹（主要针对正文/评论）
ARGUMENT_HINTS = re.compile(
    r"(because|so that|which means|the reason|we found|turned out|in my experience"
    r"|however|although|instead of|worse than|better than"
    r"|因为|所以|其实|我认为|我觉得|结果|反而|不如|代价|教训|踩坑|复盘|翻倍)",
    re.I,
)

# 标题里带立场信号（主要针对 HN 文章标题 —— 标题本身就是观点）
STANCE_HINTS = re.compile(
    r"(why |goodbye|death|dead|future|first|mistake|stop |don't|never|overrated"
    r"|back to|vs\.? |versus|better|worse|overkill|still |fallacy|harmful|not "
    r"|instead|considered|enough|rethink|problem|trap"
    r"|为什么|其实|不如|反对|别再|才怪|翻车|才是|真香|踩坑)",
    re.I,
)


def min_len_for(c: Candidate) -> int:
    """不同源给出的文本长度天然不同，阈值必须跟着源走：
        · HN 文章模式只有标题（几十字）
        · V2EX / 掘金给的是"标题 + 命中片段"（约 100 字）
        · HN 评论、GitHub issue 才是长正文

    不区分就会像之前那样：好结果被判 0 分（第一轮 HN 评论、第二轮 V2EX 都踩过这个坑）。"""
    if c.source in ("v2ex", "juejin"):
        return 40
    if (c.extra or {}).get("tags") == "story":
        return 25
    return 160


def looks_argumentative(text: str) -> bool:
    return bool(ARGUMENT_HINTS.search(text or ""))


def has_stance_signal(c: Candidate) -> bool:
    return bool(STANCE_HINTS.search(c.title or "") or STANCE_HINTS.search(c.text or ""))


def keyword_hit(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


def evaluate(cands: list[Candidate], issue: dict, anchors: list[str] | None = None) -> dict:
    """anchors = 判断"沾不沾议题"用的锚点词。默认用议题关键词，
    但议题关键词里有中文（远程办公/坐班），而 HN 是英文源，必须把检索词也算进锚点，
    否则英文候选会被全部误判为"不沾题"。"""
    anchors = anchors or issue["keywords"]
    lengths = [len(c.text) for c in cands] or [0]

    rows = []
    for c in cands:
        ok_len = len(c.text) >= min_len_for(c)
        topical = keyword_hit(c.text, anchors)
        stance = has_stance_signal(c)
        rows.append({"c": c, "ok_len": ok_len, "topical": topical, "stance": stance})

    substantial = [r for r in rows if r["ok_len"]]
    arg_hits = [r for r in substantial if looks_argumentative(r["c"].text)]
    topical = [r for r in substantial if r["topical"]]
    on_topic_stance = [r for r in topical if r["stance"]]

    per_source: dict[str, int] = {}
    for c in cands:
        per_source[c.source] = per_source.get(c.source, 0) + 1

    per_stage: dict[str, int] = {}
    for c in cands:
        key = c.stage or "未标注"
        per_stage[key] = per_stage.get(key, 0) + 1

    return {
        "unique_candidates": len(cands),
        "per_source": per_source,
        "per_stage": per_stage,
        "substantial": len(substantial),
        "argumentative": len(arg_hits),
        "topical": len(topical),
        "topical_with_stance": len(on_topic_stance),
        "median_len": int(statistics.median(lengths)),
        "usable": sorted({r["c"].url for r in on_topic_stance}),
        "usable_list": [r["c"] for r in on_topic_stance],
    }


def verdict(ev: dict) -> tuple[bool, str]:
    usable = len(ev["usable"])
    if usable >= 12:
        return True, f"可行：{usable} 条「沾议题 + 标题带立场」的候选，够支撑演示"
    if usable >= 5:
        return True, f"可用：{usable} 条候选，够跑通链路，但最好再补检索词或补源"
    if usable >= 1:
        return False, f"偏弱：只有 {usable} 条候选，需要调整检索词"
    return False, "不可行：0 条候选，必须换检索策略"


# ---------------------------------------------------------------------------
# 5. 离线自检
# ---------------------------------------------------------------------------
def selftest() -> int:
    problems: list[str] = []
    ids = [i["issue_id"] for i in ISSUE_TAXONOMY]

    for issue in ISSUE_TAXONOMY:
        sid = issue["issue_id"]
        entry = ISSUE_TERMS.get(sid)
        if entry is None:
            problems.append(f"术语表缺少议题 {sid}")
            continue

        if entry.get("retrieval") not in ("auto", "none"):
            problems.append(f"{sid} 的 retrieval 字段非法：{entry.get('retrieval')!r}")

        if not entry.get("topics"):
            problems.append(f"{sid} 缺少 topics（宽召回词）")

        sides = entry.get("sides") or {}
        for side_key in ("side_a", "side_b"):
            side = issue[side_key]
            terms = sides.get(side)
            if not terms:
                problems.append(f"{sid} 缺少 {side_key}「{side}」的检索词")
                continue
            for t in terms:
                # 实测：多词查询在 V2EX 与 HN 上都会退化（空格被当松散 OR，捞回无关内容）。
                # 所以检索词一律压到 1–2 个词，长一点的必须拆成两个词条。
                if len(t.split()) > 2:
                    problems.append(f"{sid}「{side}」检索词过长（多词会退化成松散匹配）：{t}")
                if t.strip() != t or not t:
                    problems.append(f"{sid}「{side}」检索词有首尾空格或为空：{t!r}")

        extra_sides = set(sides) - {issue["side_a"], issue["side_b"]}
        if extra_sides:
            problems.append(f"{sid} 的 sides 里有不属于本议题的立场：{extra_sides}")

    for sid in ISSUE_TERMS:
        if sid not in ids:
            problems.append(f"术语表里有自创议题：{sid}")

    for sid in NO_RETRIEVAL:
        if (ISSUE_TERMS.get(sid) or {}).get("retrieval") != "none":
            problems.append(f"{sid} 在 NO_RETRIEVAL 里但 retrieval 不是 none")

    n_auto = sum(1 for e in ISSUE_TERMS.values() if e.get("retrieval") == "auto")
    n_terms = sum(
        len(v) for e in ISSUE_TERMS.values() for k, v in (e.get("sides") or {}).items()
    )
    print("=" * 68)
    print(f"术语表自检  TERMS_VERSION={TERMS_VERSION}")
    print("=" * 68)
    print(f"议题数               {len(ISSUE_TAXONOMY)}")
    print(f"术语表覆盖           {len(ISSUE_TERMS)}")
    print(f"  可自动检索         {n_auto}")
    print(f"  人工预置（不检索） {len(NO_RETRIEVAL)}  {', '.join(NO_RETRIEVAL)}")
    print(f"检索词总数           {n_terms}")
    print("-" * 68)

    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("全部通过：两方检索词齐备、无自创议题、无长短语 ✅")

    ms = next(i for i in ISSUE_TAXONOMY if i["issue_id"] == "microservices_vs_monolith")
    assert opposite_side(ms, "a") == "单体", "对立面推导错误"
    assert opposite_side(ms, "b") == "微服务", "对立面推导错误"
    assert opposite_side(ms, "neutral") is None, "中立不该有对立面"
    print("对立面推导冒烟测试通过 ✅")
    return 0


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------
def cmd_probe(args: argparse.Namespace) -> int:
    issue = next((i for i in ISSUE_TAXONOMY if i["issue_id"] == args.probe), None)
    if issue is None:
        print(f"未知议题：{args.probe}", file=sys.stderr)
        print("可用议题：", ", ".join(i["issue_id"] for i in ISSUE_TAXONOMY), file=sys.stderr)
        return 2

    side_keys = {"a": "side_a", "b": "side_b"}
    if args.supports in (issue["side_a"], issue["side_b"]):
        side = "a" if args.supports == issue["side_a"] else "b"
    elif args.supports in side_keys:
        side = args.supports
    else:
        print(
            f"未知立场「{args.supports}」，本议题只有：{issue['side_a']} / {issue['side_b']}",
            file=sys.stderr,
        )
        return 2

    want = opposite_side(issue, side)
    if want is None:
        print("该立场没有对立面（neutral / undetermined）", file=sys.stderr)
        return 2

    print("=" * 68)
    print(f"检索可行性探针   {issue['label']}  ({issue['issue_id']})")
    print("=" * 68)
    print(f"假设库条目支持     {args.supports}")
    print(f"要去找的对立面     {want}")
    print(f"该方的检索词       {' / '.join(ISSUE_TERMS[issue['issue_id']]['sides'][want])}")
    print(f"召回档位           {args.tier}")
    print(f"检索源             {', '.join(args.sources)}   每词每源取 {args.limit} 条")
    if args.origin:
        src = ORIGIN_TO_SOURCE.get(args.origin.lower(), "（无对应适配器）")
        print(f"原条目所在的源     {args.origin} → 第一档只在 {src} 里找，"
              f"可用候选够 {args.min_usable} 条就不全网搜")
    print("-" * 68)

    entry = ISSUE_TERMS[issue["issue_id"]]
    anchors = list(issue["keywords"]) + list(entry["topics"])
    for t in entry["sides"].values():
        anchors += list(t)
    anchors = list(dict.fromkeys(a.lower() for a in anchors))

    try:
        cands = collect(
            issue["issue_id"],
            want,
            limit=args.limit,
            sources=tuple(args.sources),
            tier=args.tier,
            hn_tags=args.hn_tags,
            origin=args.origin,
            anchors=anchors,
            min_usable=args.min_usable,
        )
    except (KeyError, ValueError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    ev = evaluate(cands, issue, anchors)

    for c in cands[: args.samples * 5]:
        top = "题" if keyword_hit(c.text, anchors) else "  "
        st = "立" if has_stance_signal(c) else "  "
        stage = {"origin": "同源", "global": "全网"}.get(c.stage, c.stage or "  ")
        print(f"[{stage}·{c.source:6} | {top}{st}] {c.title[:56]}")
        print(f"           {c.url}")
        print(f"           {re.sub(chr(10) + '+', ' ', c.text)[:170]}")
        print()

    print("=" * 68)
    print("评估")
    print("=" * 68)
    print(f"候选总数（去重后）   {ev['unique_candidates']}")
    print(f"  按档位             {ev['per_stage']}")
    print(f"  按来源             {ev['per_source']}")
    print(f"文本长度达标         {ev['substantial']}")
    print(f"  含论证痕迹         {ev['argumentative']}")
    print(f"  沾议题关键词       {ev['topical']}")
    print(f"  沾题 + 带立场信号   {ev['topical_with_stance']}")
    print(f"文本长度中位数       {ev['median_len']}")
    print(f"★ 值得送进模型验证   {len(ev['usable'])}")
    ok, msg = verdict(ev)
    print("-" * 68)
    print(("✅ " if ok else "❌ ") + msg)

    if args.out:
        payload_ev = {k: v for k, v in ev.items() if k != "usable_list"}
        payload_ev["usable_candidates"] = [c.digest() for c in ev["usable_list"]]
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "issue_id": issue["issue_id"],
                    "supports": args.supports,
                    "want": want,
                    "terms_version": TERMS_VERSION,
                    "tier": args.tier,
                    "sources": list(args.sources),
                    "evaluation": payload_ev,
                    "candidates": [c.digest() for c in cands],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"原始结果已写入 {args.out}")
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    """不套任何术语表，直接把一个原始 query 丢给某个源，用来看检索精度。
    调术语表 / 试算符的时候用它，比改代码再跑快得多。"""
    token = _load_env_value("GITHUB_TOKEN")
    for source in args.sources:
        print("=" * 72)
        print(f"[{source}] {args.raw_query}")
        print("=" * 72)
        fn = ADAPTERS[source]
        try:
            if source == "github":
                batch = fn(args.raw_query, args.limit, token, "raw")
            elif source == "hn":
                batch = fn(args.raw_query, args.limit, args.hn_tags)
            else:
                batch = fn(args.raw_query, args.limit)
        except Exception as e:
            print(f"  检索失败：{e}\n")
            continue
        if not batch:
            print("  0 条结果\n")
            continue
        for c in batch:
            t = re.sub(r"\s+", " ", c.text)
            print(f"- {(c.title[:64] or '(无标题)')}   len={len(c.text)}")
            print(f"  {c.url}")
            print(f"  {t[:200]}")
        print()
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """扫一遍全部议题，输出"可检索性地图"。

    为什么要这个：实测发现不同议题的检索效果差异很大 ——
    架构类议题（微服务/单体）中文讨论多、效果好；
    生活/职场类（远程办公）和工具类（自建）用同样的方法却捞不到观点。
    演示时只该挑"可检索"的议题上，其余用人工预置兜底。这张表就是选议题的依据。
    """
    rows = []
    print("=" * 78)
    print("议题可检索性扫描（假设库条目支持 side_a，去找 side_b）")
    print("=" * 78)
    print(f"{'议题':34}{'候选':>6}{'可用':>6}  {'主要来源'}")
    print("-" * 78)

    for issue in ISSUE_TAXONOMY:
        sid = issue["issue_id"]
        entry = ISSUE_TERMS.get(sid) or {}
        if entry.get("retrieval") != "auto":
            print(f"{sid:34}{'—':>6}{'—':>6}  人工预置（不检索）")
            continue
        want = opposite_side(issue, "a")
        try:
            cands = collect(
                sid, want, limit=args.limit, sources=tuple(args.sources),
                tier=args.tier, hn_tags=args.hn_tags,
            )
        except Exception as e:
            print(f"{sid:34}{'ERR':>6}{'':>6}  {e}")
            continue

        anchors = list(issue["keywords"]) + list(entry.get("topics") or [])
        for t in (entry.get("sides") or {}).values():
            anchors += list(t)
        anchors = list(dict.fromkeys(a.lower() for a in anchors))
        ev = evaluate(cands, issue, anchors)

        srcs = ",".join(f"{k}{v}" for k, v in sorted(ev["per_source"].items()))
        ok, _ = verdict(ev)
        rows.append((sid, len(ev["usable"]), ok))
        print(f"{sid:34}{ev['unique_candidates']:>6}{len(ev['usable']):>6}  {srcs}")

    print("-" * 78)
    good = [r for r in rows if r[1] >= 5]
    mid = [r for r in rows if 1 <= r[1] < 5]
    bad = [r for r in rows if r[1] == 0]
    print(f"✅ 可检索（≥5 条可用）  {len(good):>3} 个：" + "、".join(r[0] for r in good))
    print(f"⚠️  偏弱（1–4 条）       {len(mid):>3} 个：" + "、".join(r[0] for r in mid))
    print(f"❌ 失效（0 条）        {len(bad):>3} 个：" + "、".join(r[0] for r in bad))
    print("\n演示选题建议：只挑 ✅ 里的议题，❌ 的改用人工预置反信息（保留字段位，别删）。")
    return 0


# ---------------------------------------------------------------------------
# 议题发现：从真实语料里反推"网上真的有人在吵什么"
# ---------------------------------------------------------------------------
# 背景：现有 16 个议题是**凭常识拟的**，实测下来只有 3 个能检索到足够好的对立内容。
# 原因很朴素 —— "技术圈公认存在的争论" ≠ "中文技术圈真的在讨论的争论"。
# 比如"远程办公 vs 坐班"在英文圈是热门争论（HN 上极多），中文社区里却搜不到，
# 因为大家关心的是别的事。
#
# 所以改用**数据驱动**：拿"争论标记词"去搜，捞出来的就是真实存在的争论，
# 再用领域词聚类，看哪些话题反复出现 —— 那才是候选议题。
DISCOVER_MARKERS = (
    "该不该", "要不要", "优缺点", "值得吗", "有必要",
    "劝退", "后悔", "为什么不用", "值不值", "该选哪个",
)

# 用来判断一条帖子"是不是在争论"的标记。出现任一即算。
# ⚠️ 第一版漏了"缺点"（"微服务的缺点"没被识别成争论帖），所以词表要比直觉更宽。
DEBATE_MARKERS = (
    "该不该", "要不要", "优缺点", "缺点", "不足", "值得吗", "值不值",
    "有没有必要", "有必要吗", "有必要", "劝退", "后悔", "为什么不用", "该选哪个",
    "好用吗", "坑", "踩坑", "吐槽", "争议", "撕逼", "怎么看", "如何评价",
    "还值得", "过时", "淘汰", "取代", "好不好",
)

DISCOVER_DOMAINS = (
    "中台", "低代码", "微服务", "单体", "架构", "重构", "技术债",
    "单元测试", "代码评审", "集成测试",
    "容器", "k8s", "kubernetes", "docker", "devops", "运维", "监控", "serverless",
    "云", "自建", "私有化",
    "前端", "后端", "全栈", "react", "vue", "angular", "svelte", "flutter", "小程序",
    "java", "golang", "python", "php", "rust", "c++", "node", "typescript",
    "数据库", "orm", "sql", "nosql", "redis", "mysql", "postgres",
    "设计模式", "静态类型", "动态类型", "函数式", "面向对象",
    "开源", "面试", "学历", "加班", "远程", "跳槽", "副业", "读研", "转行", "大厂",
    "copilot", "大模型", "gpt", "cursor", "ai 编程",
)


def cmd_discover(args: argparse.Namespace) -> int:
    """扫候选技术词，看哪个词的结果里"争论帖"最多 —— 用来挑真正适合做议题的话题。

    用法：python antipode_sources.py --discover --limit 8

    【为什么是"反过来扫"】
    先试过正向扫（拿"该不该/要不要"去搜，再从结果里找技术词）—— 失败。
    70 条结果里一个技术领域词都没命中：中文技术论坛里"该不该""要不要"这类措辞
    主要命中**生活决策帖**（提前还贷、送礼给室友、辞职回家、要不要提升学历），
    技术选型的争论不用这套措辞。

    所以改成：拿候选技术词逐个搜，数它的结果里有多少条标题带争论标记
    （优缺点 / 该不该 / 值得吗 / 劝退 / 后悔 / 有没有必要 …）。
    争论帖占比高的技术词 = 这个话题真的有人吵 = 适合做成议题。
    """
    print("=" * 78)
    print(f"议题发现 —— 反扫候选技术词（{len(DISCOVER_DOMAINS)} 个词，每词取 {args.limit} 条）")
    print("=" * 78)

    rows = []
    for i, term in enumerate(DISCOVER_DOMAINS, 1):
        try:
            batch = ADAPTERS["v2ex"](term, args.limit)
        except Exception as e:
            print(f"  [!] 「{term}」检索失败：{e}", file=sys.stderr)
            continue

        clean = [c for c in batch if not is_noise(c)]
        # 只认标题里真的出现该词的（sov2ex 匹配很松，不筛会全是噪声）
        topical = [c for c in clean if term.lower() in (c.title or "").lower()]
        debated = [c for c in topical if any(mk in (c.title or "") for mk in DEBATE_MARKERS)]

        if topical:
            rows.append((term, len(topical), len(debated), debated or topical))
        if i % 10 == 0:
            print(f"  ...已扫 {i}/{len(DISCOVER_DOMAINS)}", file=sys.stderr)
        time.sleep(_delay_for("v2ex", False))

    print("-" * 78)
    rows.sort(key=lambda r: (-r[2], -r[1]))
    print(f"{'候选议题词':<14}{'命中':>5}{'其中争论帖':>10}   样例标题")
    print("-" * 78)
    for term, n_top, n_deb, samples in rows:
        if n_top < args.min_count:
            continue
        flag = "★" if n_deb > 0 else " "
        print(f"{flag}{term:<13}{n_top:>5}{n_deb:>10}   {(samples[0].title or '')[:46]}")
        for c in samples[1:2]:
            print(f"{'':<14}{'':>5}{'':>10}   {(c.title or '')[:46]}")
        print(f"{'':<14}{'':>5}{'':>10}   {samples[0].url}")

    print("-" * 78)
    print("怎么用这张表：")
    print("  · ★ 且「其中争论帖」多的词 = 真实语料里反复被吵的话题，优先做议题")
    print("  · 还要能说出**两个明确的对立面**（本系统的二元结构要求）")
    print("  · 最后用 --probe <issue_id> 测可检索性，通过的才进清单")
    print("  · 光有命中、没有争论帖的词（如纯工具名）= 有人在问怎么做，不是在吵，不适合")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="反引擎检索适配器 / 可行性探针")
    ap.add_argument("--selftest", action="store_true", help="离线自检术语表，不联网")
    ap.add_argument("--probe", metavar="ISSUE_ID", help="议题 id")
    ap.add_argument("--sweep", action="store_true", help="扫全部议题，输出可检索性地图")
    ap.add_argument("--discover", action="store_true", help="从真实争论帖反推候选议题")
    ap.add_argument("--min-count", type=int, default=2, help="--discover 时至少出现几次才列出")
    ap.add_argument("--supports", help="库条目支持的立场（会去找它的对立面）")
    ap.add_argument("--sources", default=",".join(RETRIEVAL_SOURCES), help="v2ex,juejin,hn,github（默认中文优先，原因见文件头）")
    ap.add_argument("--tier", default="both", choices=("side", "topic", "both"), help="召回档位：sides 精确 / topics 宽 / both")
    ap.add_argument("--origin", default="", help="原条目所在的源（v2ex/juejin/hn/github）→ 第一档只在它里面找")
    ap.add_argument("--min-usable", type=int, default=3, help="第一档的可用候选够几条就不全网搜")
    ap.add_argument("--limit", type=int, default=5, help="每词每源取多少条")
    ap.add_argument("--samples", type=int, default=2, help="打印多少组样本")
    ap.add_argument("--list-issues", action="store_true", help="列出全部议题与术语")
    ap.add_argument("--raw-query", help="调参用：直接跑一个原始 query，不查术语表")
    ap.add_argument("--hn-tags", default="story", choices=("comment", "story"), help="HN 检索文章标题还是评论区（默认 story，实测评论区分不清对立面）")
    ap.add_argument("--out", help="把原始候选写成 JSON")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    args.sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    bad = [s for s in args.sources if s not in ADAPTERS]
    if bad:
        print(f"未知来源：{bad}", file=sys.stderr)
        return 2

    if args.raw_query:
        return cmd_raw(args)

    if args.sweep:
        return cmd_sweep(args)

    if args.discover:
        return cmd_discover(args)

    if args.list_issues:
        for i in ISSUE_TAXONOMY:
            entry = ISSUE_TERMS.get(i["issue_id"], {})
            flag = "自动检索" if entry.get("retrieval") == "auto" else "人工预置"
            print(f"{i['issue_id']:32} {i['side_a']:8} ↔ {i['side_b']:8}  [{flag}]")
            for s in (i["side_a"], i["side_b"]):
                print(f"    {s:10} → {' / '.join((entry.get('sides') or {}).get(s, [])) or '（缺）'}")
        return 0

    if args.probe:
        if not args.supports:
            print("--probe 需要同时给 --supports", file=sys.stderr)
            return 2
        return cmd_probe(args)

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
