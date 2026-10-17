#!/usr/bin/env python3
"""反仓库批量编排 —— 一个库（Vault）→ 一批反库条目。

    Vault(24 条条目) ──▶ 逐条跑引擎 ──▶ AntipodeItem[] + 一份体检报告

单条能跑 ≠ 一个库能跑。真实场景里有三类坑，这个脚本专门处理：

  ① **一口气跑会崩**：24 条串起来跑十几分钟，中间任何一条报错就前功尽弃
     → 单条失败只记账、不中断，其余照跑
  ② **跑一半断了**：网断了 / 被限流 / 机器重启，重跑要么重复劳动要么重复写入
     → `--resume` 读已有反库，按 `for_vault_item` 去重，只补没跑过的
  ③ **"跑了但没东西"说不清**：评委问"为什么这条没有反信息"，答不上来是致命的
     → 每条都落一个明确原因（中立 / 无候选 / 全被闸门拒 / 议题不可检索）

用法：
    # 看这个库会被怎么处理（不联网、不写文件）
    python run_vault.py --vault vault.example.json --items items.jsonl --dry-run

    # 正式跑（默认断点续跑）
    python run_vault.py --vault vault.example.json --items items.jsonl

    # 演示用：只跑前 3 条，每条之间停 1 秒，避免被限流
    python run_vault.py --vault vault.example.json --items items.jsonl --limit 3 --sleep 1

    # 断网兜底（内置候选，不联网、不调 API）
    python run_vault.py --vault vault.example.json --items items.jsonl --offline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from antipode_engine import build_antipode_items, Funnel  # noqa: E402
from antipode_sources import RETRIEVAL_SOURCES  # noqa: E402
from model_adapter import (  # noqa: E402
    OpenAICompatBackend, MockBackend, BatchJsonlBackend, LocalFileBackend,
    load_label_map,
)
from stance_extractor import ISSUE_TAXONOMY, resolve_side, opposite_side  # noqa: E402
from retrieval_terms import NO_RETRIEVAL  # noqa: E402

CST = timezone(timedelta(hours=8))

ISSUE_BY_ID = {i["issue_id"]: i for i in ISSUE_TAXONOMY}
ISSUE_IDS = set(ISSUE_BY_ID)

# 每条条目的归宿。反库产出之外，其余都算"没有反信息"，但原因必须分清楚 ——
# 这是答辩时"为什么这条没有"的唯一可靠答案来源。
OUTCOME_LABEL = {
    "produced": "✅ 有产出",
    "neutral": "中立立场（无可对立）",
    "no_issue": "议题不在 16 议题表内",
    "no_stance": "原文没有明确立场（无可对立）",
    "off_issue": "判出的议题与原文不符（判立场串味）",
    "no_text": "条目没有可用正文",
    "no_candidate": "检索零候选（议题不可检索）",
    "all_rejected": "候选全被闸门拒（检索到了但确实不是反方）",
    "truncated": "通过但被 top-k 截断",
    "error": "运行出错",
    "fetch_failed": "取不到正文（链接抓不到，无法判立场）",
    "disabled": "库未开启反推荐",
    "skipped": "已有记录，跳过（断点续跑）",
}


@dataclass
class Row:
    """一条库条目的处理结果。"""
    item_id: str
    title: str = ""
    issue_id: str = ""
    supports: str = ""
    want_side: str = ""
    outcome: str = ""
    produced: int = 0
    candidates: int = 0
    prescreened: int = 0
    passed: int = 0
    rejected: dict = field(default_factory=dict)
    error: str = ""
    elapsed: float = 0.0

    def reason_line(self) -> str:
        if self.outcome == "produced":
            base = f"{self.produced} 条（候选 {self.candidates} → 送检 {self.prescreened} → 通过 {self.passed}）"
        elif self.outcome == "all_rejected" and self.rejected:
            detail = " / ".join(f"{k} {v}" for k, v in sorted(self.rejected.items(), key=lambda x: -x[1]))
            base = f"候选 {self.candidates} → 送检 {self.prescreened} 全拒（{detail}）"
        elif self.outcome == "no_candidate":
            base = f"0 候选 —— 该议题在当前中文源上没有可召回内容"
        elif self.outcome == "fetch_failed":
            base = self.error[:88]
        elif self.outcome == "error":
            base = self.error[:80]
        else:
            base = OUTCOME_LABEL.get(self.outcome, self.outcome)
        return base


# ---------------------------------------------------------------------------
# 读入
# ---------------------------------------------------------------------------
def load_vault(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        vault = json.load(fh)
    if not isinstance(vault, dict):
        raise SystemExit(f"❌ 库文件格式不对（应为 JSON 对象）：{path}")
    return vault


def load_items(vault_path: str, items_path: str, vault: dict) -> list[dict]:
    """条目来源优先级：显式 --items > 库文件内嵌 items > 同名 .jsonl。"""
    if items_path:
        return _read_jsonl(items_path)

    inline = vault.get("items")
    if isinstance(inline, list) and inline:
        return inline

    guess = os.path.splitext(vault_path)[0] + ".items.jsonl"
    if os.path.exists(guess):
        return _read_jsonl(guess)

    raise SystemExit(
        "❌ 找不到库里的条目。三种给法任选一种：\n"
        "   ① --items <条目.jsonl>\n"
        "   ② 库 JSON 里内嵌 \"items\": [...]\n"
        f"   ③ 在库文件旁边放一份同名文件：{os.path.basename(guess)}"
    )


def _read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"❌ {path} 第 {n} 行不是合法 JSON：{e}") from e
    return out


def _normalize_items(items: list[dict]) -> list[dict]:
    """把"只有 URL"的条目补齐成能跑的条目。

    队友只给原文章地址是**常态**，不是异常。所以这里做三件小事：
      ① 没 id → 用链接的 sha1 前 10 位造一个（稳定、可复现，重跑能对上）
      ② 没 source → 从域名推断（三档检索的第一档要知道"原条目在哪个源"）
      ③ 没 title → 留空，等取正文时补（正文里的 h1 / <title> 更准）
    """
    try:
        from vault_fetch import guess_source
    except Exception:                                            # noqa: BLE001
        guess_source = lambda u: ""                              # noqa: E731

    out: list[dict] = []
    for it in items:
        it = dict(it)
        url = str(it.get("url") or it.get("source_url") or "").strip()
        if url and not it.get("url"):
            it["url"] = url
        if not str(it.get("id") or "").strip():
            if not url:
                out.append(it)            # 没 id 也没链接 → 交给 preflight 报错
                continue
            it["id"] = "u-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        if url and not str(it.get("source") or "").strip():
            it["source"] = guess_source(url)
        out.append(it)
    return out


def load_done(path: str) -> dict[str, int]:
    """已有的反库文件 → {库条目 id: 已产出条数}。用于断点续跑。"""
    done: dict[str, int] = {}
    if not path or not os.path.exists(path):
        return done
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue          # 上次写一半断电的行，跳过即可
            k = d.get("for_vault_item")
            if k:
                done[k] = done.get(k, 0) + 1
    return done


# ---------------------------------------------------------------------------
# 一票否决：先筛掉不该跑的条目（省时间也省 API 钱）
# ---------------------------------------------------------------------------
def preflight(item: dict) -> str:
    """返回空串 = 可以跑；否则返回不跑的 outcome。"""
    if not str(item.get("id") or "").strip():
        return "error"
    issue_id = str(item.get("issue_id") or "")
    if issue_id and issue_id not in ISSUE_IDS:
        return "no_issue"
    # 梗 / 生活偏好类（Tab vs 空格、豆腐脑甜咸）只做人工预置演示，不进检索链路。
    # 不在这里拦的话，会在 collect() 里抛异常，被记成"运行出错"—— 分类错了。
    if issue_id in NO_RETRIEVAL:
        return "no_issue"
    supports = str(item.get("supports") or "").strip().lower()
    if supports in ("neutral", "中立", "none", "null"):
        return "neutral"
    has_text = any(str(item.get(k) or "").strip() for k in ("full_text", "raw_excerpt", "summary"))
    has_url = bool(str(item.get("url") or "").strip())
    # ★ 只有链接也算能跑 —— 正文由模块 ⓪ 去取。这是"队友只给 URL"的默认路径。
    if not has_text and not has_url:
        return "no_text"
    return ""


def classify(items: list[dict], f: Funnel) -> str:
    """跑完之后：这条为什么有 / 没有反信息。"""
    if items:
        return "produced"
    if f.candidates == 0:
        return "no_candidate"
    if f.passed == 0:
        return "all_rejected"
    return "truncated"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="反仓库批量编排：一个库 → 一批反库条目")
    ap.add_argument("--vault", required=True, help="库（Vault）JSON 文件")
    ap.add_argument("--items", default="", help="条目 JSONL（不给则读库内嵌 items 或同名文件）")
    ap.add_argument("--out", default="", help="反库输出路径（默认 data/antipode_<vault_id>.jsonl）")
    ap.add_argument("--stats", default="", help="体检报告输出路径（默认与反库同名的 .stats.json）")
    ap.add_argument("--backend", default="openai_compat",
                    choices=["openai_compat", "mock", "batch_jsonl", "local_file"])
    ap.add_argument("--labels", default="", help="batch_jsonl 的标签文件")
    ap.add_argument("--module", default="", help="local_file 的模块路径")
    ap.add_argument("--sources", default=",".join(RETRIEVAL_SOURCES))
    ap.add_argument("--offline", action="store_true", help="用内置候选，不联网")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（演示用）")
    ap.add_argument("--sleep", type=float, default=0.0, help="每条之间停多少秒（防限流）")
    ap.add_argument("--max-verify", type=int, default=8)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--min-score", type=float, default=0.7)
    ap.add_argument("--origin-bonus", type=float, default=1.25)
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--no-resume", action="store_true", help="不跳过已处理过的条目（会重复写入）")
    ap.add_argument("--cookies", default="", metavar="PATH",
                    help="登录态 Cookie 文件（JSON）。用在反爬站点：用户自己登录后导出，"
                         "只放本地、不进仓库。见 code/cookies.example.json")
    ap.add_argument("--dry-run", action="store_true", help="只看会处理哪些条目，不联网、不写文件")
    ap.add_argument("--quiet", action="store_true", help="不逐条打印引擎漏斗")
    args = ap.parse_args()

    if args.cookies:
        os.environ["ANTIPODE_COOKIES"] = os.path.abspath(args.cookies)

    vault = load_vault(args.vault)
    vault_id = str(vault.get("vault_id") or "default")
    items = _normalize_items(load_items(args.vault, args.items, vault))

    out_path = args.out or os.path.join(_ROOT, "data", f"antipode_{vault_id}.jsonl")
    stats_path = args.stats or os.path.splitext(out_path)[0] + ".stats.json"

    print("=" * 78)
    print(f"反仓库批量编排   库 {vault_id}"
          f"   主题 {vault.get('topic') or '(未标)'}   条目 {len(items)} 条")
    print("=" * 78)

    # 开关：反推荐是库级选择，不是默认行为
    if vault.get("antipode_enabled") is False:
        print("🔕 这个库没有开启反推荐（antipode_enabled=false）—— 尊重设置，一条都不生成。")
        print("   邮件、代码 issue 这类私人事务/工作材料的库，就应该保持这个状态。")
        return 0

    if args.limit and len(items) > args.limit:
        print(f"※ 演示模式：只处理前 {args.limit} 条（共 {len(items)} 条）")
        items = items[: args.limit]

    done = {} if args.no_resume else load_done(out_path)
    if done:
        print(f"※ 断点续跑：已有 {len(done)} 条库条目处理过，将跳过")

    # ---------- 先做一票否决，把不该跑的摘出来 ----------
    rows: list[Row] = []
    todo: list[dict] = []
    for it in items:
        r = Row(item_id=str(it.get("id") or "?"), title=str(it.get("title") or ""),
                issue_id=str(it.get("issue_id") or ""), supports=str(it.get("supports") or ""))
        if r.item_id in done:
            r.outcome, r.produced = "skipped", done[r.item_id]
            rows.append(r)
            continue
        verdict = preflight(it)
        if verdict:
            r.outcome = verdict
            r.error = "" if verdict != "error" else "条目缺少 id 字段"
            rows.append(r)
            continue
        todo.append(it)

    print(f"\n可跑 {len(todo)} 条 / 先筛掉 {len(items) - len(todo)} 条")
    if args.dry_run:
        print("-" * 78)
        for it in todo:
            side = str(it.get("supports") or "")
            want = ""
            issue = ISSUE_BY_ID.get(str(it.get("issue_id") or ""))
            if side and issue:
                try:
                    want = opposite_side(issue, resolve_side(issue, side))
                except Exception:                                   # noqa: BLE001
                    want = ""
            print(f"  ▶ {it.get('id'):<22} {(it.get('title') or it.get('url') or '')[:32]:<34}"
                  f" {side or '(待判)':<10} →  对立面 {want or '（待引擎推导）'}")
        print("-" * 78)
        for r in rows:
            if r.outcome != "skipped":
                print(f"  ✕ {r.item_id:<22} {r.title[:34]:<36} {OUTCOME_LABEL.get(r.outcome, r.outcome)}")
        print(f"\n试运行结束。正式跑去掉 --dry-run 即可，产出会写到：{out_path}")
        return 0

    # ---------- 后端 ----------
    if args.backend == "mock":
        backend = MockBackend()
    elif args.backend == "batch_jsonl":
        backend = BatchJsonlBackend(args.labels, load_label_map(
            os.path.join(_HERE, "label_map.example.json")))
    elif args.backend == "local_file":
        backend = LocalFileBackend(args.module, label_map=load_label_map(
            os.path.join(_HERE, "label_map.example.json")))
    else:
        backend = OpenAICompatBackend()

    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())

    # ---------- 逐条跑 ----------
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    mode = "a" if (done and not args.no_resume) else "w"
    total_new = 0
    out_fh = open(out_path, mode, encoding="utf-8")
    try:
        for n, it in enumerate(todo, 1):
            iid = str(it.get("id") or "?")
            r = Row(item_id=iid, title=str(it.get("title") or ""),
                    issue_id=str(it.get("issue_id") or ""), supports=str(it.get("supports") or ""))
            print(f"\n[{n}/{len(todo)}] {iid}  {r.title[:44]}")
            t0 = time.time()
            try:
                produced, f = build_antipode_items(
                    it, vault, backend=backend, sources=sources,
                    origin=str(it.get("source") or ""),
                    max_verify=args.max_verify, top_k=args.top_k,
                    offline=args.offline, origin_bonus=args.origin_bonus,
                    enrich=not args.no_enrich, min_score=args.min_score,
                    verbose=not args.quiet,
                )
                r.outcome = classify(produced, f)
                r.produced = len(produced)
                r.candidates = f.candidates
                r.prescreened = f.prescreened
                r.passed = f.passed
                r.rejected = dict(f.rejected)
                r.want_side = f.want_side
                if produced:
                    for p in produced:
                        out_fh.write(json.dumps(p, ensure_ascii=False) + "\n")
                    out_fh.flush()          # 每条都落盘：中途崩了也不丢已出的结果
                    total_new += len(produced)
            except RuntimeError as e:
                # 引擎的可预期拒绝（中立、无正文、开关关）—— 不算故障
                msg = str(e)
                r.outcome = ("fetch_failed" if "取不到正文" in msg
                             else "off_issue" if "串味" in msg
                             # 「议题是梗/生活偏好」「与 16 个议题都不沾边」：
                             # 这是"这条本来就不该做反推荐"，不是程序出错。
                             # 实测踩过：ValueError 冒到外层，10 条里 3 条被记成"运行出错"，
                             # 看着像 bug，其实是模型硬挑了豆腐脑议题。
                             else "no_issue" if ("不做反推荐" in msg or "不沾边" in msg)
                             else "no_stance" if "未能从库条目" in msg
                             else "neutral" if "对立面" in msg or "中立" in msg
                             else "no_text" if "正文" in msg or "文本" in msg else "error")
                r.error = msg
            except Exception as e:                                  # noqa: BLE001
                r.outcome = "error"
                r.error = f"{type(e).__name__}: {e}"
                if os.environ.get("ANTIPODE_DEBUG"):
                    traceback.print_exc()
            r.elapsed = round(time.time() - t0, 1)
            rows.append(r)
            print(f"    → {OUTCOME_LABEL.get(r.outcome, r.outcome)}   {r.reason_line()}   "
                  f"({r.elapsed}s)")
            if args.sleep:
                time.sleep(args.sleep)
    finally:
        out_fh.close()

    # ---------- 汇总 ----------
    # 按库条目的原始顺序输出（预筛掉的条目也要待在原位，读者才看得懂）
    order = {str(it.get("id") or "?"): n for n, it in enumerate(items)}
    rows.sort(key=lambda r: order.get(r.item_id, 1 << 30))

    print("\n" + "=" * 78)
    print("批量结果")
    print("=" * 78)
    print(f"{'库条目':<24}{'议题':<30}{'结果':>6}  {'说明'}")
    print("-" * 78)
    for r in rows:
        issue = r.issue_id or "(待判)"
        print(f"{r.item_id[:23]:<24}{issue[:29]:<30}{r.produced:>6}  {r.reason_line()[:60]}")
    print("-" * 78)

    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    total_produced = sum(r.produced for r in rows)
    # 命中率的分母：真正送进检索的条目（预筛掉的和跳过的都没有机会，不该算进分母）
    ran = [r for r in rows if r.outcome not in
          ("skipped", "neutral", "no_stance", "off_issue", "no_issue", "no_text", "disabled",
           "fetch_failed")]
    hit = (by_outcome.get("produced", 0) / len(ran)) if ran else 0
    # 累计：此前跑过且有产出的 + 本次有产出的
    cum_ids = {k for k, v in done.items() if v > 0} | {
        r.item_id for r in rows if r.outcome == "produced"}
    cum_total = sum(r.produced for r in rows) + sum(
        v for k, v in done.items() if k not in {r.item_id for r in rows})

    print(f"条目 {len(rows)} 条　有产出 {by_outcome.get('produced', 0)} 条　"
          f"无产出 {len(rows) - by_outcome.get('produced', 0)} 条　"
          f"出错 {by_outcome.get('error', 0)} 条")
    print("原因分布：" + "　".join(
        f"{OUTCOME_LABEL.get(k, k)} {v}" for k, v in sorted(by_outcome.items(), key=lambda x: -x[1])))
    print(f"反库条目 {cum_total} 条（本次新增 {total_new} 条）　"
          f"本次命中率 {hit:.0%}（送检 {len(ran)} 条）　"
          f"全库累计 {len(cum_ids)}/{len(rows)} 条有反信息")
    print(f"产出文件 {out_path}")
    if total_produced:
        print(f"平均每条库条目带出 {total_produced / by_outcome.get('produced', 1):.1f} 条反信息")

    stats = {
        "vault_id": vault_id,
        "vault_topic": vault.get("topic", ""),
        "ran_at": datetime.now(CST).isoformat(timespec="seconds"),
        "mode": "offline" if args.offline else "online",
        "items_total": len(rows),
        "items_with_output": by_outcome.get("produced", 0),
        "antipode_items": total_produced,
        "hit_rate": round(hit, 3),
        "outcome_distribution": by_outcome,
        "rows": [
            {
                "item_id": r.item_id, "title": r.title, "issue_id": r.issue_id,
                "supports": r.supports, "want_side": r.want_side,
                "outcome": r.outcome, "produced": r.produced,
                "candidates": r.candidates, "prescreened": r.prescreened,
                "passed": r.passed, "rejected": r.rejected,
                "error": r.error, "elapsed_s": r.elapsed,
            }
            for r in rows
        ],
    }
    with open(stats_path, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)
    print(f"体检报告 {stats_path}")
    print("\n※ 反库独立存放，与队友的库不混在一起 —— 靠 for_vault_item 建对应关系。")
    return 0 if total_produced else 3


if __name__ == "__main__":
    raise SystemExit(main())
