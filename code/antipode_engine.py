#!/usr/bin/env python3
"""反仓库引擎 —— 把模块 0-3 串成一条端到端流水线。

    库条目(VaultItem) ──▶ 反库条目(AntipodeItem)

    ① 判立场       这条信息站在议题哪一方          （模块 0 · 复用 3.3）
    ② 取对立面     对立面 = 符号运算，不是感觉      （纯规则）
    ③ 检索候选     同源优先 → 全网                  （模块 1 · 三档优先级）
    ④ 二次判立场   ★ 确认候选真的站对立面           （模块 0 再跑一次）
    ⑤ 省流         给卡片生成一句话                 （模型，失败则抽取式兜底）
    ⑥ 写反库       按契约输出 JSONL，独立存放        （纯规则）

【为什么第 ④ 步不能省】
实测过：关键词检索**分不清立场，甚至系统性偏向一侧** ——
搜 `return to office` 捞回来的全是反对 RTO 的文章。
所以检索层只负责"召回"，"这条到底站哪一方"必须让模型再判一次。
不做这一步，反库里会有约 3/4 是噪声。

用法：
    # 离线自检（不联网、不调用 API）
    python antipode_engine.py --selftest

    # 完整跑一条：库条目 → 反库条目
    python antipode_engine.py --json vault_item.example.json --origin v2ex

    # 现场断网兜底（用内置候选，不联网）
    python antipode_engine.py --json vault_item.example.json --offline --mock

    # 看漏斗每一步的数字（默认就打印）
    python antipode_engine.py --json vault_item.example.json --report-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from stance_extractor import (  # noqa: E402
    ISSUE_TAXONOMY, resolve_side, opposite_side, verify_evidence, normalize,
)
from passage_selector import select_from_vault_item  # noqa: E402
from antipode_sources import (  # noqa: E402
    collect, is_usable_candidate, keyword_hit, has_stance_signal, Candidate,
    RETRIEVAL_SOURCES,
)
from retrieval_terms import ISSUE_TERMS  # noqa: E402
from model_adapter import (  # noqa: E402
    OpenAICompatBackend, BatchJsonlBackend, LocalFileBackend, MockBackend, Judgment,
)

ISSUE_BY_ID = {i["issue_id"]: i for i in ISSUE_TAXONOMY}
CST = timezone(timedelta(hours=8))

# counter_type：P0 只做 A 类（观点对立），B/C 类留字段位
COUNTER_TYPE = "opposing_view"


# ---------------------------------------------------------------------------
# 离线兜底候选（现场断网时用；内容是真实形态的样例，不是编造的）
# ---------------------------------------------------------------------------
FIXTURE_CANDIDATES = [
    {
        "source": "v2ex", "candidate_id": "v2ex-demo-1",
        "url": "https://www.v2ex.com/t/645719",
        "title": "微服务的缺点",
        "text": "微服务的缺点 微服务火了好几年，我们当初看中的是拆分之后每个项目更小、"
                "每个人需要了解的东西更少。但实际做下来，一个请求跨好几个服务，"
                "排障时间翻了三倍，链路追踪搭了三个月才勉强能用。对十来人的团队，"
                "这笔交换不划算。",
        "query": "微服务", "stage": "origin", "popularity": 81,
    },
    {
        "source": "v2ex", "candidate_id": "v2ex-demo-2",
        "url": "https://www.v2ex.com/t/1007047",
        "title": "2024 年了，有多少公司和系统由微服务转为单体架构？",
        "text": "2024 年了，有多少公司和系统由微服务/云原生转为了单体架构？"
                "最近看脉脉里有不少开发讲自己所在企业的系统已经开始由微服务转回传统单体架构。",
        "query": "单体", "stage": "origin", "popularity": 0,
    },
    {
        "source": "v2ex", "candidate_id": "v2ex-demo-3",
        "url": "https://www.v2ex.com/t/999999",
        "title": "单体应用改造微服务疑惑求解",
        "text": "长期以来公司架构是基于 Spring 的单体应用，对于新需求也能快速响应。"
                "但是由于业务多元化，单体应用开始吃力，我们在考虑拆分。",
        "query": "单体", "stage": "origin", "popularity": 12,
    },
]


def _fixture() -> list[Candidate]:
    out = []
    for c in FIXTURE_CANDIDATES:
        out.append(Candidate(**c))
    return out


# ---------------------------------------------------------------------------
# 漏斗统计 —— 每一步的数字都要能对得上账
# ---------------------------------------------------------------------------
@dataclass
class Funnel:
    vault_id: str = ""
    vault_item_id: str = ""
    issue_id: str = ""
    vault_supports: str = ""
    vault_side: str = ""
    stance_from: str = ""            # vault（队友已标）/ model（我抽的）
    stance_calls: int = 0
    want_side: str = ""
    candidates: int = 0
    per_stage: dict = field(default_factory=dict)
    per_source: dict = field(default_factory=dict)
    prescreened: int = 0
    enriched: int = 0        # 补过论证素材的候选数（HN 文章只有标题）
    verify_calls: int = 0
    passed: int = 0          # 通过二次判立场的条数
    truncated: int = 0       # 通过但因超出 top_k 没输出的条数
    accepted: int = 0        # 最终输出的条数
    rejected: dict = field(default_factory=dict)
    summary_from: dict = field(default_factory=dict)
    output_path: str = ""
    mode: str = ""

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def report(self) -> str:
        L = []
        L.append("=" * 72)
        L.append(f"反仓库引擎   库条目 {self.vault_item_id or '(未命名)'}"
                 + (f"   vault={self.vault_id}" if self.vault_id else ""))
        L.append("=" * 72)
        if self.mode:
            L.append(f"运行模式       {self.mode}")

        src = {"vault": "队友已标注（省一次模型调用）", "model": f"我自己抽的（{self.stance_calls} 次调用）"}.get(
            self.stance_from, self.stance_from)
        L.append(f"① 判立场       {self.vault_supports}  [{self.vault_side}]   ← {src}")
        L.append(f"② 取对立面     {self.want_side}")
        L.append(f"③ 检索候选     {self.candidates} 条   "
                 f"档位 {self.per_stage}   来源 {self.per_source}")
        L.append(f"   粗筛送检     {self.prescreened} 条（按沾题 + 立场信号排序）")
        if self.enriched:
            L.append(f"   补论证素材   {self.enriched} 条（HN 文章只有标题 → 取其讨论评论）")
        L.append(f"④ 二次判立场   {self.verify_calls} 次调用 → 通过 {self.passed} 条")
        if self.rejected:
            detail = " / ".join(f"{k} {v}" for k, v in sorted(self.rejected.items()))
            L.append(f"   拒因分布     {detail}")
        if self.truncated:
            L.append(f"   截断         {self.truncated} 条（通过但超出 top-k，未输出）")
        if self.summary_from:
            L.append(f"⑤ 省流         {self.summary_from}")
        L.append(f"⑥ 写入反库     {self.accepted} 条 → {self.output_path or '(未写)'}")
        L.append("-" * 72)
        if self.candidates:
            keep = self.accepted / self.candidates * 100
            L.append(f"漏斗收束       {self.candidates} → {self.prescreened} → {self.accepted}"
                     f"（最终保留 {keep:.1f}%）")
        L.append("=" * 72)
        return "\n".join(L)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def build_anchors(issue_id: str) -> list[str]:
    """判断候选"沾不沾题"用的锚点词。"""
    issue = ISSUE_BY_ID.get(issue_id) or {}
    anchors = list(issue.get("keywords") or [])
    entry = ISSUE_TERMS.get(issue_id) or {}
    anchors += list(entry.get("topics") or [])
    for v in (entry.get("sides") or {}).values():
        anchors += list(v)
    return list(dict.fromkeys(a.lower() for a in anchors if a))


def first_sentence(text: str, limit: int = 80) -> str:
    parts = re.split(r"(?<=[。！？!?])\s*", text.strip())
    s = next((p for p in parts if p.strip()), text.strip())
    return s[:limit]


def extractive_summary(text: str, limit: int = 70) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def make_summary(text: str, backend) -> tuple[str, str]:
    """生成反库条目的省流。返回 (摘要, 来源)。

    用模型生成更好读；失败或后端不支持时退化为抽取式（取前两句），
    并如实标注来源 —— 不假装两种一样。
    """
    if isinstance(backend, OpenAICompatBackend) and not isinstance(backend, MockBackend):
        try:
            from openai import OpenAI

            key = os.environ.get("OPENAI_API_KEY", "").strip()
            base = os.environ.get("OPENAI_BASE_URL", "").strip()
            model = os.environ.get("STANCE_MODEL", "").strip()
            if key and model:
                client = OpenAI(api_key=key, base_url=base or None)
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content":
                               "把下面这段内容压缩成一句话中文摘要，不超过 60 字。"
                               "只输出摘要本身，不要任何前缀或引号。\n\n" + text[:1500]}],
                    temperature=0,
                )
                s = (resp.choices[0].message.content or "").strip().strip('"“”')
                if 8 <= len(s) <= 120:
                    return s, "model"
        except Exception as e:
            print(f"    [!] 省流调用失败，退化为抽取式：{e}", file=sys.stderr)
    return extractive_summary(text), "extractive"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_antipode_items(
    vault_item: dict,
    vault: dict | None = None,
    backend=None,
    sources: tuple[str, ...] = RETRIEVAL_SOURCES,
    origin: str = "",
    max_verify: int = 8,
    top_k: int = 3,
    origin_bonus: float = 1.25,
    enrich: bool = True,
    offline: bool = False,
    report_only: bool = False,
    verbose: bool = True,
) -> tuple[list[dict], Funnel]:
    f = Funnel(mode="离线兜底（不联网）" if offline else "联机（真实检索）")
    backend = backend or OpenAICompatBackend()

    # 开关：反推荐是用户的选择，不是默认行为
    if vault is not None and vault.get("antipode_enabled") is False:
        raise RuntimeError(
            f"库 {vault.get('vault_id')} 未开启反推荐（antipode_enabled=false）—— 尊重设置，不生成。"
        )
    if vault:
        f.vault_id = str(vault.get("vault_id") or "")
    f.vault_item_id = str(vault_item.get("id") or "")

    # ---------- ① 判立场（库条目这一侧） ----------
    issue_id = str(vault_item.get("issue_id") or "")
    supports = str(vault_item.get("supports") or "")

    if issue_id and supports and issue_id in ISSUE_BY_ID:
        f.stance_from = "vault"
    else:
        passages, pstats = select_from_vault_item(vault_item)
        if not passages:
            raise RuntimeError("库条目里没有任何可用文本（full_text / raw_excerpt / summary 都空）")
        if verbose:
            print(f"  [库条目] 精选 {len(passages)} 段 / 压缩比 {pstats['compression']}"
                  f"（读的字段：{pstats.get('source_field')}）", file=sys.stderr)
        texts = [p.text for p in passages]
        judgments = backend.judge(texts, "")
        f.stance_calls += len(texts)
        f.stance_from = "model"

        best: Judgment | None = None
        for j in judgments:
            if j.side in ("a", "b") and (best is None or j.confidence > best.confidence):
                best = j
        if best is None:
            raise RuntimeError("未能从库条目里判出明确立场（都是中立/判不出）—— 这条不该进反仓库")
        issue_id, supports = best.issue_id, best.supports

    issue = ISSUE_BY_ID[issue_id]
    side = resolve_side(issue, supports)
    f.issue_id, f.vault_supports, f.vault_side = issue_id, supports, side

    # ---------- ② 取对立面 ----------
    want = opposite_side(issue, side)
    if want is None:
        raise RuntimeError(f"立场 {supports} 是中立/判不出，没有对立面 —— 不进反仓库")
    f.want_side = want
    expect_side = "b" if side == "a" else "a"

    # ---------- ③ 检索候选 ----------
    anchors = build_anchors(issue_id)
    if offline:
        cands = _fixture()
    else:
        cands = collect(
            issue_id, want, origin=origin, anchors=anchors,
            sources=sources, min_usable=3,
        )
    f.candidates = len(cands)
    for c in cands:
        f.per_stage[c.stage or "未标注"] = f.per_stage.get(c.stage or "未标注", 0) + 1
        f.per_source[c.source] = f.per_source.get(c.source, 0) + 1

    # 粗筛：分三档排队，保证送检的是最像样的一批。
    #
    # ⚠️ 单列"问句标题"这一档：实测「单体服务中，模块之间相互调用怎么优雅地实现？」
    # 这种**技术求助帖**会被模型判成"支持单体"（置信度 0.95）—— 就因为标题里有"单体服务"。
    # 但提问不是立场表达。判据与 passage_selector 的"纯提问扣分"保持一致：
    # 不直接丢（有些问句标题的正文里有明确结论），但只在非问句候选不够时才启用。
    QUESTION_TITLE = re.compile(r"[?？]\s*$")
    is_q = lambda c: bool(QUESTION_TITLE.search(c.title or ""))
    ok = lambda c: is_usable_candidate(c, anchors)
    to_verify = ([c for c in cands if ok(c) and not is_q(c)]
                 + [c for c in cands if ok(c) and is_q(c)]
                 + [c for c in cands if not ok(c)])[:max_verify]
    f.prescreened = len(to_verify)
    if verbose:
        n_ok = sum(1 for c in cands if ok(c))
        print(f"  [检索] 候选 {len(cands)} 条，其中 {n_ok} 条沾题带立场；送检 {len(to_verify)} 条",
              file=sys.stderr)

    # 补论证素材：HN 文章模式只有标题，拿它当证据等于把"声明"当"论据"。
    # 这里给送检的候选补上该文章的讨论评论，后面会**逐条判**并挑出站对立面的那条。
    if enrich and not offline:
        from antipode_sources import enrich_candidate

        enriched_list = []
        for c in to_verify:
            before = bool((c.extra or {}).get("enriched"))
            up = enrich_candidate(c)
            if (up.extra or {}).get("enriched") and not before:
                f.enriched += 1
                if verbose:
                    print(f"  [补素材] {up.title[:44]} → +{up.extra.get('comments_used')} 条讨论"
                          f"（{len(c.text)} → {len(up.text)} 字）", file=sys.stderr)
            enriched_list.append(up)
        to_verify = enriched_list

    # ---------- ④ 二次判立场（本环节是整个引擎的精度来源） ----------
    #
    # 两条路径：
    #   A. 候选带论证素材（HN 文章已补评论）→ **逐条评论判**，挑出真正站对立面的那条当证据。
    #      为什么不在整段上判：讨论区天然是两派在吵，整段判容易得到"中立"，
    #      而且拿不出"到底是哪句话"—— 逐条判才能给出可点开验证的证据。
    #   B. 候选自带正文（V2EX 片段等）→ 整条判，证据优先用模型给的片段。
    accepted: list[dict] = []
    for c in to_verify:
        if not keyword_hit(c.text, anchors):
            f.reject("不沾题")
            continue

        comments = (c.extra or {}).get("comments") or []
        picked: dict | None = None
        last_js: list[Judgment] = []

        if comments:
            for cm in comments[:3]:
                try:
                    js = backend.judge([cm["text"]], issue_id)
                except Exception as e:
                    print(f"    [!] 判评论失败：{e}", file=sys.stderr)
                    f.reject("调用失败")
                    continue
                f.verify_calls += len(js) or 1
                last_js = js
                hit = next((j for j in js if j.issue_id == issue_id and j.side == expect_side), None)
                if hit:
                    picked = {
                        "hit": hit,
                        "span": first_sentence(cm["text"]),
                        "ev_source": "hn_comment",
                        "ev_url": cm.get("url") or c.url,
                        "excerpt": re.sub(r"\s+", " ", cm["text"]).strip()[:400],
                    }
                    break

        if picked is None:
            try:
                js = backend.judge([c.text], issue_id)
            except Exception as e:
                print(f"    [!] 判立场失败：{e}", file=sys.stderr)
                f.reject("调用失败")
                continue
            f.verify_calls += len(js) or 1
            last_js = js
            hit = next((j for j in js if j.issue_id == issue_id and j.side == expect_side), None)
            if hit is None:
                sides = {j.side for j in js} or {"unknown"}
                if sides <= {"unknown"}:
                    f.reject("判不出")
                elif sides & {"neutral", "undetermined"}:
                    f.reject("中立")
                else:
                    f.reject("站错边")
                continue
            span = (hit.evidence_span or "").strip()
            if span and verify_evidence(c.text, span):
                ev_source = "model"
            else:
                span = first_sentence(c.text)
                ev_source = "candidate_text"
            if not verify_evidence(c.text, span):
                f.reject("证据缺失")
                continue
            # 问句标题的候选：如果模型只能拿标题本身当证据，说明它在**正文里**
            # 拿不出立场句 —— 那就是个技术求助帖，不是观点表达。
            # （实测：「单体服务中，模块之间相互调用怎么优雅地实现？」被判支持单体，0.95）
            if is_q and normalize(span) == normalize(c.title or ""):
                f.reject("只有提问，正文无立场")
                continue
            picked = {
                "hit": hit,
                "span": span,
                "ev_source": ev_source,
                "ev_url": c.url,
                "excerpt": re.sub(r"\s+", " ", c.text).strip()[:400],
            }

        accepted.append({"c": c, **picked})

    # 排序：按**质量问题**（对立度），同源只给温和加成。
    #
    # ⚠️ 这里连踩两个坑，记录一下：
    #   1. 第一版只按对立度排 → "第一档 origin"被架空：检索时优先了同源，
    #      但 HN 标题本身就是完整立场（置信度 0.95~1.0），排序时把同源全压下去，
    #      最终三条全来自全局档。
    #   2. 第二版改成"同源硬优先级" → 又走到另一个极端：把一条对立度只有 0.60 的
    #      同源候选，排在了两条 1.00 的全局候选前面，**质量换来了纯度**。
    #
    # 最终理解（也更符合用户原意）：**"第一档"管"去哪儿找"，不管"最终选谁"。**
    # 所以：检索严格三档；排序仍按质量，同源给 1.25 倍温和加成
    # （0.60×1.25=0.75 < 1.00，压不过真正的强对立；但 0.85×1.25=1.06 > 1.00，能赢）。
    rank = lambda t: -(t["hit"].confidence * (origin_bonus if t["c"].stage == "origin" else 1.0))
    accepted.sort(key=rank)
    f.passed = len(accepted)
    if len(accepted) > top_k:
        f.truncated = len(accepted) - top_k
    accepted = accepted[:top_k]
    f.accepted = len(accepted)

    # ---------- ⑤⑥ 省流 + 写反库 ----------
    items: list[dict] = []
    for i, a in enumerate(accepted, 1):
        c, hit = a["c"], a["hit"]
        # 省流基于**证据原文**（有评论就用评论），而不是整段混合讨论
        summary, sum_src = make_summary(a["excerpt"], backend)
        f.summary_from[sum_src] = f.summary_from.get(sum_src, 0) + 1
        items.append({
            "id": f"anti-{f.vault_item_id}-{i}",
            "for_vault_item": f.vault_item_id,
            "issue_id": issue_id,
            "counter_type": COUNTER_TYPE,
            "takes_side": want,
            "url": c.url,
            # 证据本身的永久链接 —— 点开就能看到那句话（HN 评论天然带永久链接）
            "evidence_url": a["ev_url"],
            "title": c.title,
            "summary": summary,
            "raw_excerpt": a["excerpt"],
            "evidence_span": a["span"],
            "opposition_score": round(float(hit.confidence), 3),
            "argument_score": None,
            "synthesized": False,
            "gate": {
                "accepted": True,
                "checked": [
                    "source_url_present",
                    "topic_matched",
                    "opposite_side_confirmed",
                    "evidence_verified",
                ],
                "evidence_source": a["ev_source"],
                "stance_confidence": round(float(hit.confidence), 3),
                "backend": hit.backend,
            },
            "found_by": {
                "stage": c.stage or "unknown",
                "source": c.source,
                "query": c.query,
                "candidate_id": c.candidate_id,
                "enriched": bool((c.extra or {}).get("enriched")),
                "discussion_url": (c.extra or {}).get("discussion_url", ""),
                "retrieved_at": now_iso(),
            },
        })
    return items, f


def write_vault(items: list[dict], path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------
def selftest() -> int:
    print("=" * 72)
    print("反仓库引擎 —— 离线自检（不联网、不调用 API）")
    print("=" * 72)
    problems: list[str] = []

    vault_item = {
        "id": "demo-1",
        "url": "https://github.com/example-org/order-service/issues/482",
        "summary": "作者复盘了单体到微服务的迁移，结论是值得做。",
        "full_text": "我们把下单链路拆成七个服务之后，发布节奏确实快了很多。"
                     "新人也不再需要先读懂整个仓库。过程很痛苦，但两年下来我的结论是："
                     "这次迁移是值得的。",
        "issue_id": "microservices_vs_monolith",
        "supports": "微服务",
    }
    vault = {"vault_id": "demo", "antipode_enabled": True}

    # 1) 正常跑通
    items, f = build_antipode_items(
        vault_item, vault, backend=MockBackend(), offline=True, verbose=False
    )
    checks = [
        ("判立场来自队友标注（省调用）", f.stance_from == "vault", f.stance_from),
        ("对立面推导正确", f.want_side == "单体", f.want_side),
        ("离线候选能读到", f.candidates == len(FIXTURE_CANDIDATES), str(f.candidates)),
        ("产出了反库条目", len(items) >= 1, f"{len(items)} 条"),
        ("每条都带真实 URL", all(i["url"].startswith("http") for i in items), ""),
        ("每条都有证据且校验通过", all(i["gate"]["evidence_source"] for i in items), ""),
        ("takes_side 等于对立面", all(i["takes_side"] == "单体" for i in items), ""),
        ("counter_type 是 A 类", all(i["counter_type"] == "opposing_view" for i in items), ""),
        ("synthesized 全部为 false", all(i["synthesized"] is False for i in items), ""),
        ("found_by 记录了来源可追溯", all(i["found_by"]["source"] for i in items), ""),
        ("省流有来源标注", all(k in ("model", "extractive") for k in f.summary_from), str(f.summary_from)),
        ("漏斗账目对得上（送检 = 通过 + 拒）",
         f.passed + sum(f.rejected.values()) == f.prescreened,
         f"{f.passed}+{sum(f.rejected.values())} vs {f.prescreened}"),
        ("输出 = 通过 - 截断", f.accepted + f.truncated == f.passed,
         f"{f.accepted}+{f.truncated} vs {f.passed}"),
    ]

    # 2) 开关：库未开启反推荐时必须拒绝
    try:
        build_antipode_items(vault_item, {"vault_id": "x", "antipode_enabled": False},
                             backend=MockBackend(), offline=True, verbose=False)
        checks.append(("库未开启反推荐 → 拒绝生成", False, "竟然跑了"))
    except RuntimeError as e:
        checks.append(("库未开启反推荐 → 拒绝生成", "未开启反推荐" in str(e), str(e)[:40]))

    # 3) 库条目是中立立场时不该产反库条目
    neutral_item = dict(vault_item, supports="neutral")
    try:
        build_antipode_items(neutral_item, vault, backend=MockBackend(), offline=True, verbose=False)
        checks.append(("中立立场 → 拒绝生成", False, "竟然跑了"))
    except RuntimeError as e:
        checks.append(("中立立场 → 拒绝生成", "对立面" in str(e), str(e)[:40]))

    for name, ok, note in checks:
        print(f"{'✅' if ok else '❌'} {name}" + (f"   （{note}）" if note else ""))
        if not ok:
            problems.append(name)

    print("-" * 72)
    print(f.report())
    print()
    if items:
        print("样例产出（第一条）：")
        print(json.dumps(items[0], ensure_ascii=False, indent=2)[:900])
        print()
    if problems:
        print(f"❌ 自检未通过：{problems}")
        return 1
    print("✅ 全部通过")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="反仓库引擎：库条目 → 反库条目")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", help="库条目 JSON 文件")
    ap.add_argument("--vault-json", help="库（Vault）JSON 文件，用于检查 antipode_enabled")
    ap.add_argument("--origin", default="", help="原条目所在的源（三档优先级第一档）")
    ap.add_argument("--backend", default="openai_compat",
                    choices=["openai_compat", "mock", "batch_jsonl", "local_file"])
    ap.add_argument("--labels", default="", help="batch_jsonl 的标签文件")
    ap.add_argument("--module", default="", help="local_file 的模块路径")
    ap.add_argument("--sources", default=",".join(RETRIEVAL_SOURCES))
    ap.add_argument("--offline", action="store_true", help="用内置候选，不联网")
    ap.add_argument("--max-verify", type=int, default=8)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--origin-bonus", type=float, default=1.25,
                    help="排序时对同源候选的加成倍数（1.0 = 不加成）")
    ap.add_argument("--no-enrich", action="store_true", help="不补论证素材（HN 讨论评论）")
    ap.add_argument("--out", default="", help="输出路径（默认 data/antipode_<vault_id>.jsonl）")
    ap.add_argument("--report-only", action="store_true", help="只打印漏斗，不写文件")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.json:
        ap.print_help()
        return 0

    with open(args.json, "r", encoding="utf-8") as fh:
        vault_item = json.load(fh)
    vault = None
    if args.vault_json and os.path.exists(args.vault_json):
        with open(args.vault_json, "r", encoding="utf-8") as fh:
            vault = json.load(fh)

    if args.backend == "mock":
        backend = MockBackend()
    elif args.backend == "batch_jsonl":
        from model_adapter import load_label_map
        backend = BatchJsonlBackend(args.labels, load_label_map(
            os.path.join(_HERE, "label_map.example.json")))
    elif args.backend == "local_file":
        from model_adapter import load_label_map
        backend = LocalFileBackend(args.module, label_map=load_label_map(
            os.path.join(_HERE, "label_map.example.json")))
    else:
        backend = OpenAICompatBackend()

    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())

    try:
        items, f = build_antipode_items(
            vault_item, vault, backend=backend, sources=sources, origin=args.origin,
            max_verify=args.max_verify, top_k=args.top_k, offline=args.offline,
            origin_bonus=args.origin_bonus, enrich=not args.no_enrich,
        )
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    if not args.report_only:
        out = args.out or os.path.join(
            os.path.dirname(_HERE), "data",
            f"antipode_{f.vault_id or 'default'}.jsonl",
        )
        f.output_path = write_vault(items, out)
    print(f.report())
    if items and not args.report_only:
        print("\n反库条目预览（第 1 条）：")
        print(json.dumps(items[0], ensure_ascii=False, indent=2))
    return 0 if items else 3


if __name__ == "__main__":
    raise SystemExit(main())
