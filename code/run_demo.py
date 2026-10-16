#!/usr/bin/env python3
"""一键跑出演示用的反库数据。

给演示准备三样东西（对应评审"成果呈现与答辩"那 40 分）：
    ① 一份真实产出的反库文件（含证据、URL、来源标注）→ data/antipode_demo.jsonl
    ② 一条现场能重跑的命令 → python run_demo.py
    ③ 一份漏斗报告 → 讲清每一步拒掉了什么、为什么

只用三个**实测可检索**的议题（详见 docs/反仓库检索_实测记录.md §8.4 的可检索性地图）：
    microservices_vs_monolith  微服务 vs 单体
    tdd_necessity              测试驱动开发是否必要
    react_vs_vue               React vs Vue

其余议题在演示中走"人工预置反信息"（字段位保留，不删）。

⚠️ 关于现场断网：这个脚本跑完产出的 `data/antipode_demo.jsonl` **已经提交进仓库**，
所以现场网络出问题时，直接打开那份文件讲解即可 —— 里面的每条都有真实 URL 可点。
这比现场硬跑更稳。另外还有 `--offline`（用内置候选，不联网）作为兜底。

用法：
    python run_demo.py                # 跑全部三个议题
    python run_demo.py --only ms      # 只跑一个
    python run_demo.py --offline      # 断网兜底
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from antipode_engine import build_antipode_items, Funnel  # noqa: E402
from model_adapter import OpenAICompatBackend, MockBackend  # noqa: E402

CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 演示用的库条目（模拟队友的产出；字段与 docs/接口契约 一致）
# 注意每条**同时带 summary 与 full_text** —— 契约里要求判立场用原文，不用省流版
# ---------------------------------------------------------------------------
DEMO_VAULT = {"vault_id": "demo", "topic": "技术选型", "antipode_enabled": True}

DEMO_ITEMS: dict[str, dict] = {
    "ms": {
        "id": "demo-ms-1",
        "source": "v2ex",
        "url": "https://github.com/example-org/order-service/issues/482",
        "title": "我们把单体拆成微服务，两年后的复盘",
        "summary": "作者复盘了单体到微服务的迁移：拆出七个服务后各小组可以独立发布、新人上手更快，结论是这次迁移值得做。",
        "full_text": (
            "我们把下单链路拆成七个服务之后，发布节奏确实快了很多，每个小组可以自己决定什么时候上线，"
            "不用再等一个大版本。新人也不再需要先读懂整个仓库，只要看懂自己负责的那两个服务就够了。\n\n"
            "当然过程很痛苦，第一年我们几乎一直在处理服务之间的边界问题，光是链路追踪就搭了三个月。"
            "但两年下来我的结论是：这次迁移是值得的。我们团队从八个人长到二十个人，"
            "如果还是单体，光是合并冲突就够呛。"
        ),
        "issue_id": "microservices_vs_monolith",
        "supports": "微服务",
    },
    "tdd": {
        "id": "demo-tdd-1",
        "source": "v2ex",
        "url": "https://github.com/example-org/billing/issues/88",
        "title": "坚持 TDD 三年，我的真实体会",
        "summary": "作者坚持测试先行三年，认为 TDD 显著降低了回归缺陷，虽然前期慢但长期回本。",
        "full_text": (
            "我们组从三年前开始强制测试先行，一开始确实很别扭，写测试的时间比写实现还长，"
            "领导也质疑过效率。\n\n"
            "但坚持半年之后差别就出来了：每次重构我都敢动手，因为一跑测试就知道有没有破坏既有行为。"
            "线上回归缺陷从平均每月四五个降到一年两三个。我认为先写测试这件事，"
            "短期看是拖慢，长期看是唯一能让重构变得安全的方式。"
        ),
        "issue_id": "tdd_necessity",
        "supports": "先写测试",
    },
    "react": {
        "id": "demo-rv-1",
        "source": "v2ex",
        "url": "https://github.com/example-org/console/issues/210",
        "title": "为什么我们最终选了 React",
        "summary": "作者在 React 与 Vue 之间做了选型，最终因为生态与招人成本选了 React。",
        "full_text": (
            "选型时我们把两边都做了原型。Vue 的上手确实更快，模板语法对后端同学更友好，"
            "文档也是中文优先的。\n\n"
            "但我们最终的判断是：生态和招人成本更关键。我们需要的几个复杂组件（可视化编辑器、"
            "虚拟滚动表格）在 React 生态里都有成熟方案，而 Vue 那边要么没有、要么维护不活跃。"
            "另外我们所在的城市，简历里写 React 的候选人是写 Vue 的三倍。所以我认为"
            "在团队规模会持续扩大的前提下，React 是更稳的选择。"
        ),
        "issue_id": "react_vs_vue",
        "supports": "React",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description="跑出演示用的反库数据")
    ap.add_argument("--only", default="", choices=["", "ms", "tdd", "react"])
    ap.add_argument("--offline", action="store_true", help="用内置候选，不联网")
    ap.add_argument("--backend", default="openai_compat", choices=["openai_compat", "mock"])
    ap.add_argument("--max-verify", type=int, default=8)
    ap.add_argument("--top-k", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(_HERE), "data", "antipode_demo.jsonl"))
    args = ap.parse_args()

    keys = [args.only] if args.only else list(DEMO_ITEMS)
    backend = MockBackend() if (args.backend == "mock" or args.offline) else OpenAICompatBackend()

    all_items: list[dict] = []
    rows: list[tuple[str, Funnel]] = []

    for k in keys:
        item = DEMO_ITEMS[k]
        print("\n" + "█" * 72)
        print(f"█ 库条目：{item['title']}   （支持「{item['supports']}」）")
        print("█" * 72)
        try:
            items, f = build_antipode_items(
                item, DEMO_VAULT, backend=backend, origin=item.get("source", "v2ex"),
                max_verify=args.max_verify, top_k=args.top_k, offline=args.offline,
            )
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            continue
        print(f.report())
        for it in items:
            print(f"   · [{it['found_by']['source']}] {it['title'][:56]}")
            print(f"     {it['url']}")
            print(f"     证据：{it['evidence_span'][:88]}")
        all_items.extend(items)
        rows.append((f.issue_id, f))

    if not all_items:
        print("\n⚠️ 没有产出任何反库条目。")
        return 3

    # 合并写一份完整反库
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for it in all_items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")

    print("\n" + "=" * 72)
    print("演示数据汇总")
    print("=" * 72)
    print(f"{'议题':32}{'候选':>6}{'送检':>6}{'通过':>6}{'输出':>6}")
    print("-" * 72)
    for issue_id, f in rows:
        print(f"{issue_id:32}{f.candidates:>6}{f.prescreened:>6}{f.passed:>6}{f.accepted:>6}")
    print("-" * 72)
    total_c = sum(f.candidates for _, f in rows)
    total_o = sum(f.accepted for _, f in rows)
    print(f"{'合计':32}{total_c:>6}{sum(f.prescreened for _, f in rows):>6}"
          f"{sum(f.passed for _, f in rows):>6}{total_o:>6}")
    print()
    print(f"★ 反库文件：{args.out}（{len(all_items)} 条，全部带真实 URL 与证据）")
    print(f"  生成时间：{datetime.now(CST).isoformat(timespec='seconds')}")
    print("  现场断网也能演示 —— 这份文件已提交进仓库，直接打开讲解即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
