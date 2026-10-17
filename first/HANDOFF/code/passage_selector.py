#!/usr/bin/env python3
"""立场段落粗筛 —— 从原文里挑出"值得判立场"的段落。

【这个模块的定位】
它是**召回器**，不是判定器 —— 纯规则、零模型调用、零网络请求。
判断"这段到底站哪一方"是 stance_extractor 的事，这里只负责"别漏、也别把整篇丢过去"。

【为什么需要它】两个实测过的理由：
  1. **整篇原文不能直接丢给模型**：几千字的长文往往同时讨论好几个议题，
     实测出现过"一次输出多条、其中几条议题归错"（把 TS 严格模式判成静态 vs 动态类型）。
     而且更长 = 更贵 = 更慢。
  2. **V2EX / HN 的候选只有一百字**，切段毫无意义。
     所以文本短于 SHORT_TEXT_LIMIT 时直接整段返回，不做任何切分。

【筛选思路】
一段话值得判立场，通常因为它**在下判断**（我认为 / 其实 / 结果 / 反而 / 后悔）
或者**在讲第一手经验**（我们公司 / 我用了 / 我们后来 / 迁移 / 改回）。
纯知识陈述（"配置 Nginx 的 upstream 模块可以……"）不是立场，丢掉。

判据分两档：
    强信号 —— 出现 1 次即可入选（作者明确在下判断，或第一手经验）
    弱信号 —— 需要出现 2 次以上（因果 / 转折 / 成本类词汇，单独出现太容易误召）
反向信号：整段只是提问、且没有任何强信号 → 扣分（提问不是立场）

用法：
    # 离线自检（内置样例，不联网）
    python passage_selector.py --selftest

    # 直接喂一段文本
    python passage_selector.py --text "我们去年上了微服务……"

    # 从库条目 JSON 里取 full_text 来筛（真实用法）
    python passage_selector.py --json code/vault_item.example.json

    # 看逐段的判定过程（调参用）
    python passage_selector.py --json xxx.json --show-all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 文本短于这个长度就不切了 —— V2EX / HN 候选都是一百字左右，切开反而丢上下文
SHORT_TEXT_LIMIT = 400

# 单段超过这个长度就按句子再切一次，避免一段几千字整块进去
MAX_PASSAGE_CHARS = 900
# 按句子切分后的目标块大小
SENTENCE_CHUNK_CHARS = 600
# 太短的段落不要（导航栏、标题残片、"继续阅读"之类）
MIN_PASSAGE_CHARS = 20

# ---------------------------------------------------------------------------
# 信号词
# ---------------------------------------------------------------------------
# 强信号：作者在明确下判断，或在讲第一手经验 —— 命中 1 次就值得送进模型
# 注意语序：中文里"后来我们"和"我们后来"都常见，只写一个会漏（自检抓到过这个 bug）
STRONG_SIGNALS = re.compile(
    r"("
    r"我认为|我觉得|我的结论|我的看法|在我看来|我建议|我的经验"
    r"|其实|实际上|事实上|说白了|坦白说|说实话"
    r"|反而|还不如|倒不如|不如直接"
    r"|代价|踩坑|踩过的坑|踩了坑|教训|复盘|翻车|坑了|被坑"
    r"|后悔|劝退|不推荐|不值得|没必要|过度设计|过度|银弹|神话|炒作"
    r"|不划算|划不来|亏了|得不偿失"
    r"|我们公司|我们团队|我们当初|我用了|我们试过|我经历过|我们踩过"
    r"|后来我们|后来把|后来就|后来发现|我们后来"
    r"|这不是说|而是说|不是.{0,8}而是"
    r"|如果重来|要是重来|再让我选|我会先|应该先|早该"
    r"|才明白|才发现|才知道|总结|结论是"
    r"|结果发现|结果却|最后我们|事后"
    r"|in my experience|we found|turned out|i think|i believe|the lesson|we learned"
    r"|avoid|never use|don't use|regret"
    r")",
    re.I,
)

# 弱信号：单独出现说明不了什么，要求一段里出现 ≥2 个
WEAK_SIGNALS = re.compile(
    r"("
    r"因为|所以|由于|导致|因此|这就意味着|意味着"
    r"|但是|不过|然而|可是|另一方面|相反"
    r"|成本|复杂度|维护|排障|故障|稳定性|效率|性能|人力"
    r"|迁移|拆分|改回|换成|替换|回退|改造"
    r"|相比|比较|优于|不如|好处|坏处|优点|缺点"
    r"|because|however|although|but |cost|complexity|maintenance|migrat|switch"
    r")",
    re.I,
)

# 程序性段落标记：配置说明、安装步骤、参数清单 —— 这类段落**没有立场**，
# 但很容易凑够两个弱信号（"配置""部署"随手就能碰上"故障""导致"）。
# 自检里就是这样误召了一段 Nacos 集群配置说明，所以加了这条惩罚规则。
PROCEDURAL_SIGNALS = re.compile(
    r"("
    r"配置|部署|安装|参数|示例|代码|命令|字段|接口文档|文档|版本|步骤"
    r"|注意|设置|开启|关闭|默认|可选|支持|兼容|quorum|集群"
    r")",
    re.I,
)

# 反向信号：整段只在提问 —— 提问不是立场
QUESTION_SIGNAL = re.compile(r"[?？]\s*$")


@dataclass
class Passage:
    index: int
    text: str
    score: int
    strong: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)
    selected: bool = False
    merged_from: int = 1

    @property
    def length(self) -> int:
        return len(self.text)

    def digest(self) -> dict:
        d = asdict(self)
        d["length"] = self.length
        return d


# ---------------------------------------------------------------------------
# 1. 切段
# ---------------------------------------------------------------------------
def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；!?;])\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def split_passages(text: str) -> list[str]:
    """自适应切段。

    三级退化策略，因为真实原文的排版千奇百怪：
      1. 先按空行切（Markdown / 博客最常见）
      2. 切出来段落太少（<3）→ 说明原文没有空行，改按单换行切
      3. 单个段落仍然过长（>900 字）→ 按句子贪心打包
    """
    if not text or not text.strip():
        return []

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]

    if len(paras) < 3:
        paras = [p.strip() for p in text.split("\n") if p.strip()]

    out: list[str] = []
    for p in paras:
        if len(p) <= MAX_PASSAGE_CHARS:
            out.append(p)
            continue
        # 过长段落按句子贪心打包
        buf = ""
        for s in split_sentences(p):
            if buf and len(buf) + len(s) > SENTENCE_CHUNK_CHARS:
                out.append(buf.strip())
                buf = s
            else:
                buf += s
        if buf.strip():
            out.append(buf.strip())

    return [p for p in out if len(p) >= MIN_PASSAGE_CHARS]


# ---------------------------------------------------------------------------
# 2. 打分
# ---------------------------------------------------------------------------
def score_passage(text: str) -> tuple[int, list[str], list[str]]:
    strong = sorted(set(m.group(0) for m in STRONG_SIGNALS.finditer(text)))
    weak = sorted(set(m.group(0) for m in WEAK_SIGNALS.finditer(text)))
    score = 2 * len(strong) + len(weak)
    # 纯提问且没有任何强信号 → 不是立场，扣分
    if QUESTION_SIGNAL.search(text.strip()) and not strong:
        score -= 1
    return score, strong, weak


def is_selected(text: str, strong: list[str], weak: list[str]) -> bool:
    """入选条件（偏向召回，因为漏掉真观点比多送一段更贵）：

        1. 有强信号 → 入选（作者明确在下判断，或第一手经验）
        2. 弱信号 ≥2 且这段不是程序性说明 → 入选
           · 为什么弱信号要 ≥2："成本""复杂度"在纯技术段落里也很常见
           · 为什么要排程序性段落：自检里一段 Nacos 配置说明靠"集群+故障"凑够了 2 个弱信号

    这里多送几段只是多花几次模型调用（免费档），漏掉一段就少一条反信息 —— 所以宁可宽松。
    """
    if strong:
        return True
    if len(weak) < 2:
        return False
    if len(PROCEDURAL_SIGNALS.findall(text)) >= 2:
        return False
    return True


def select_passages(
    text: str,
    max_passages: int = 12,
    merge_adjacent: bool = True,
    min_score: int = 0,
) -> tuple[list[Passage], dict[str, Any]]:
    """主入口：返回（选中的段落列表, 统计信息）。"""
    raw = split_passages(text)

    # 短文本快速通道：V2EX / HN 的候选就是一百字，整段返回
    if len(text.strip()) <= SHORT_TEXT_LIMIT:
        p = Passage(index=0, text=text.strip(), score=0, selected=True)
        stats = _stats(text, raw, [p], short_circuit=True)
        return [p], stats

    passages: list[Passage] = []
    for i, t in enumerate(raw):
        score, strong, weak = score_passage(t)
        passages.append(Passage(index=i, text=t, score=score, strong=strong, weak=weak))

    for p in passages:
        p.selected = is_selected(p.text, p.strong, p.weak) and p.score > min_score

    # 相邻段落如果都入选，合并 —— 保留上下文，别把论证切成碎片
    if merge_adjacent:
        merged: list[Passage] = []
        i = 0
        while i < len(passages):
            cur = passages[i]
            if not cur.selected:
                i += 1
                continue
            j = i
            while j + 1 < len(passages) and passages[j + 1].selected:
                j += 1
            if j > i:
                block = passages[i : j + 1]
                merged.append(
                    Passage(
                        index=cur.index,
                        text="\n\n".join(b.text for b in block),
                        score=sum(b.score for b in block),
                        strong=sorted({s for b in block for s in b.strong}),
                        weak=sorted({w for b in block for w in b.weak}),
                        selected=True,
                        merged_from=len(block),
                    )
                )
            else:
                merged.append(cur)
            i = j + 1
        selected = merged
    else:
        selected = [p for p in passages if p.selected]

    if len(selected) > max_passages:
        selected = sorted(selected, key=lambda p: -p.score)[:max_passages]
        selected.sort(key=lambda p: p.index)

    stats = _stats(text, raw, selected)
    stats["all_passages"] = [p.digest() for p in passages]
    return selected, stats


def _stats(original: str, all_paras: list[str], selected: list[Passage], short_circuit: bool = False) -> dict:
    sel_chars = sum(p.length for p in selected)
    total = len(original.strip())
    return {
        "original_chars": total,
        "paragraph_count": len(all_paras),
        "selected_count": len(selected),
        "selected_chars": sel_chars,
        "compression": round(sel_chars / total, 3) if total else 0.0,
        "short_circuit": short_circuit,
    }


def select_from_vault_item(item: dict, max_passages: int = 12) -> tuple[list[Passage], dict]:
    """从库条目里挑段落。读取顺序（见接口契约 §2.2）：
        full_text → raw_excerpt → summary
    注意 summary 只用于定位议题，不该拿来判立场；但总比什么都没有强，
    所以放在最后兜底，并在 stats 里标出来，便于事后审计。"""
    source_field = None
    text = ""
    for f in ("full_text", "raw_excerpt", "summary"):
        if item.get(f):
            text = item[f]
            source_field = f
            break
    selected, stats = select_passages(text, max_passages=max_passages)
    stats["source_field"] = source_field
    stats["degraded"] = source_field != "full_text"
    return selected, stats


# ---------------------------------------------------------------------------
# 3. 离线自检
# ---------------------------------------------------------------------------
DEMO_TEXT = """我们把下单链路拆成七个服务，这件事到今天已经两年了。

拆分本身在技术上并不复杂。我们用的是 Spring Cloud 全家桶，服务注册发现走
Nacos，配置中心也是 Nacos，网关用 Spring Cloud Gateway。每个服务独立一个
代码仓库，独立一条 CI 流水线，独立一套 Dockerfile。

但是问题很快就来了。上线之后第一次大促，一个下单请求要跨七个服务，链路追踪
我们搭了整整三个月才勉强能用。排障时间翻了三倍 —— 以前在单体里看一眼日志就
能定位的问题，现在要在七个服务的日志里挨个找。

我认为这笔交换对当时只有八个人的团队来说是不划算的。我们本想用微服务换
开发效率，结果换来的是运维复杂度。如果重来一次，我会先做模块化单体。

后来我们把订单和库存两个服务合并回去了，排障时间立刻降了一半。这不是说微
服务不好，而是说它的前提是组织规模够大 —— 康威定律不是一句口号。

配置 Nacos 集群时需要注意三个节点的 quorum 设置，建议不要把三个节点部署在
同一台物理机上，否则机器故障会导致整个注册中心不可用。

服务划分的粒度建议参考业务能力，而不是技术分层。常见做法是先按领域驱动设计
划出限界上下文，再把每个上下文映射成一个或几个服务，这一过程需要和业务方
反复确认。

网关层我们用的是 Spring Cloud Gateway，鉴权统一放在网关做，下游服务只信任
内部令牌。熔断用的是 Resilience4j，配置了慢调用比例和异常比例的阈值。

监控方面，指标走 Prometheus，日志走 Loki，链路走 SkyWalking。三套系统的
告警规则都配置在同一个告警平台，避免告警风暴。

总结：架构选型要看团队规模，而不是看别人在用什么。
"""
# ↑ 注意：这段样例里同时有"观点段"和"纯技术段"，用来验证筛选器能不能区分两者。
#   中间那几段（Nacos 配置 / 服务划分 / 网关 / 监控）都是**故意放进去的技术说明**，
#   如果它们被选中，说明筛选器在把"没有立场的段落"也送进模型，属于误召。


def selftest() -> int:
    problems: list[str] = []

    selected, stats = select_passages(DEMO_TEXT)
    texts = [p.text for p in selected]
    joined = "\n".join(texts)

    checks = [
        ("切段正常（≥10 段）", stats["paragraph_count"] >= 10, f"实际 {stats['paragraph_count']}"),
        # 注意：这里数的是**合并后**的块数。相邻的观点段会被合并成一块
        # （样例里前三段观点是连着的，正确行为是合成 1 块，所以下限是 2 而不是 3）
        ("选出块数在 2–5 之间", 2 <= stats["selected_count"] <= 5, f"实际 {stats['selected_count']}"),
        ("压缩比 < 0.6", stats["compression"] < 0.6, f"实际 {stats['compression']}"),
        ("抓到了「我认为」那段", "我认为这笔交换" in joined, ""),
        ("抓到了「排障时间翻了三倍」", "排障时间翻了三倍" in joined, ""),
        ("抓到了「后来我们把订单和库存」", "把订单和库存两个服务合并" in joined, ""),
        ("抓到了作者结论段", "架构选型要看团队规模" in joined, ""),
        ("没抓纯配置说明段", "quorum" not in joined, "配置段不该入选"),
        ("没抓网关/监控等技术段", "SkyWalking" not in joined, "技术段不该入选"),
    ]

    print("=" * 72)
    print("立场段落粗筛 —— 离线自检")
    print("=" * 72)
    for name, ok, note in checks:
        print(f"{'✅' if ok else '❌'} {name}" + (f"   （{note}）" if note else ""))
        if not ok:
            problems.append(name)

    # 短文本快速通道
    short = "微服务拆得太细，排障时间翻了三倍。"
    sel_short, st_short = select_passages(short)
    ok = st_short["short_circuit"] and len(sel_short) == 1
    print(f"{'✅' if ok else '❌'} 短文本走快速通道，整段返回")
    if not ok:
        problems.append("短文本快速通道")

    print("-" * 72)
    print(f"原文字数 {stats['original_chars']}  →  选出 {stats['selected_chars']} 字"
          f"（{stats['compression'] * 100:.0f}%）")
    print(f"段落 {stats['paragraph_count']} → 选中 {stats['selected_count']}")
    print()
    for p in selected:
        head = re.sub(r"\s+", " ", p.text)[:60]
        print(f"  · [{p.score:>3}分 强{len(p.strong)}/弱{len(p.weak)} 合并{p.merged_from}] {head}……")
    print()
    if problems:
        print(f"❌ 自检未通过，问题：{problems}")
        return 1
    print("✅ 全部通过")
    return 0


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    stats: dict = {}
    if args.json:
        with open(args.json, "r", encoding="utf-8") as f:
            item = json.load(f)
        selected, stats = select_from_vault_item(item, max_passages=args.max)
        label = f"{os.path.basename(args.json)}  字段={stats.get('source_field')}"
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
        selected, stats = select_passages(text, max_passages=args.max)
        label = os.path.basename(args.file)
    else:
        selected, stats = select_passages(args.text or "", max_passages=args.max)
        label = "命令行文本"

    print("=" * 72)
    print(f"立场段落粗筛   {label}")
    print("=" * 72)
    print(f"原文字数      {stats['original_chars']}")
    print(f"切出段落数    {stats['paragraph_count']}")
    print(f"选中段落数    {stats['selected_count']}")
    print(f"选中字数      {stats['selected_chars']}")
    print(f"压缩比        {stats['compression'] * 100:.0f}%   ← 省下的就是模型成本")
    if stats.get("short_circuit"):
        print("文本过短，走快速通道（未切分）")
    if stats.get("degraded"):
        print(f"⚠️ 没有 full_text，退化为读取 {stats.get('source_field')}，判定质量会下降")
    print("-" * 72)

    for p in selected:
        print(f"\n【第 {p.index} 段 | {p.score} 分 | 强{len(p.strong)} 弱{len(p.weak)}"
              f"{' | 合并了' + str(p.merged_from) + '段' if p.merged_from > 1 else ''}】")
        if p.strong:
            print(f"  强信号：{'、'.join(p.strong)}")
        if p.weak:
            print(f"  弱信号：{'、'.join(p.weak)}")
        print("  " + re.sub(r"\n+", "\n  ", p.text)[:800])

    if args.show_all:
        print("\n" + "=" * 72)
        print("逐段判定明细")
        print("=" * 72)
        for p in stats.get("all_passages", []):
            mark = "✔" if p["selected"] else "·"
            head = re.sub(r"\s+", " ", p["text"])[:64]
            print(f"{mark} [{p['score']:>3}分] {head}……")
            if p["strong"]:
                print(f"      强：{'、'.join(p['strong'])}")
            if p["weak"]:
                print(f"      弱：{'、'.join(p['weak'])}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(
                {"stats": {k: v for k, v in stats.items() if k != "all_passages"},
                 "passages": [p.digest() for p in selected]},
                f, ensure_ascii=False, indent=2,
            )
        print(f"\n结果已写入 {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从原文里挑出值得判立场的段落（纯规则，不调用模型）")
    ap.add_argument("--selftest", action="store_true", help="离线自检，不联网")
    ap.add_argument("--text", help="直接传一段文本")
    ap.add_argument("--file", help="从文本文件读")
    ap.add_argument("--json", help="从库条目 JSON 读 full_text")
    ap.add_argument("--max", type=int, default=12, help="最多保留几段（默认 12）")
    ap.add_argument("--show-all", action="store_true", help="打印逐段判定明细")
    ap.add_argument("--out", help="把选中段落写成 JSON")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not (args.text or args.file or args.json):
        ap.print_help()
        return 0
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
