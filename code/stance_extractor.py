"""
立场抽取模块 (Stance Extraction)
=================================
输入: 一段文本
输出: 该文本对预定义议题的立场 + 原文证据

设计要点:
  1. 议题必须来自预定义清单 (不许自创), 保证每个议题都有确定的二元对立结构
  2. 强制模型输出 evidence_span, 用代码校验它是否真的是原文子串 → 防幻觉
  3. 置信度三级分档: accept / review / reject
  4. 长文本分块 + 重叠 + 合并
  5. 结果按内容哈希缓存

依赖: pip install openai
用法: python stance_extractor.py
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from typing import Any

# 自动加载同目录下的 .env (需 pip install python-dotenv)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# 0. 议题清单 —— 唯一需要人工维护的东西
# ---------------------------------------------------------------------------
# 只列"技术圈有真实争论"的议题。每个议题必须能明确说出正反两方是什么。
ISSUE_TAXONOMY: list[dict[str, str]] = [
    {"issue_id": "microservices_vs_monolith", "label": "微服务 vs 单体", "domain": "软件架构"},
    {"issue_id": "tdd_necessity", "label": "测试驱动开发是否必要", "domain": "工程实践"},
    {"issue_id": "monorepo_vs_multirepo", "label": "单体仓库 vs 多仓库", "domain": "工程实践"},
    {"issue_id": "react_vs_vue", "label": "React vs Vue", "domain": "前端"},
    {"issue_id": "rest_vs_graphql", "label": "REST vs GraphQL", "domain": "接口设计"},
    {"issue_id": "orm_or_raw_sql", "label": "ORM 还是手写 SQL", "domain": "数据访问"},
    {"issue_id": "typescript_strictness", "label": "TypeScript 严格模式是否值得", "domain": "前端"},
]

TAXONOMY_VERSION = "v1"  # 清单或 prompt 变更时递增, 用于缓存失效

# ---------------------------------------------------------------------------
# 1. Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是立场分析器。给定一段文本和一份议题清单,判断文本对这些议题的立场。

【硬性规则】
1. issue_id 必须来自给定清单,不许自创。如果一个都不匹配,返回空数组。
2. 立场是文本【实际表达】的立场,不是文本作者的自我声明。
3. evidence_span 必须是原文的【精确子串】,一个字都不能改。做不到就降低 confidence。
4. 遇到讽刺、反话、阴阳怪气,按【实际含义】判断,不要按字面意思。
5. 如果文本只是在【引用】或【转述】他人观点而没有自己表态,标为 undetermined。
6. 如果文本对多个议题都有立场,全部输出。
7. 中立地讨论正反两面而不倾向任何一方,标为 neutral。
8. 注意【隐含立场】:没有直说"我支持/反对",但字里行间态度明确的,要判出来。

【polarity 取值】
- pro: 支持 target
- con: 反对 target
- neutral: 中立讨论,不倾向
- undetermined: 仅转述他人观点,作者未表态

【confidence 取值】0 到 1。证据不充分时必须给低分,不要硬编。

只输出 JSON,不要任何其他文字。"""

# Few-shot 示例, 覆盖 5 种最难的情况
FEW_SHOT: list[tuple[str, dict[str, Any]]] = [
    # 1. 直接支持
    (
        "我们把单体拆成微服务之后,部署频率从每月一次提升到每天十次,团队可以独立发布,这是巨大的收益。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "target": "microservices",
                    "polarity": "pro",
                    "strength": "strong",
                    "confidence": 0.95,
                    "evidence_span": "部署频率从每月一次提升到每天十次",
                    "reasoning": "明确陈述收益",
                }
            ]
        },
    ),
    # 2. 隐含反对 (最难的一类)
    (
        "我们去年上了微服务,结果光是链路追踪就搭了三个月,一个下单请求要跨七个服务,排障时间翻了三倍。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "target": "microservices",
                    "polarity": "con",
                    "strength": "strong",
                    "confidence": 0.85,
                    "evidence_span": "一个下单请求要跨七个服务,排障时间翻了三倍",
                    "reasoning": "未直接表态,但罗列的全是负面后果,隐含反对",
                }
            ]
        },
    ),
    # 3. 讽刺反话
    (
        "微服务真是太棒了——我们花了两周才让一个健康检查正常工作。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "target": "microservices",
                    "polarity": "con",
                    "strength": "moderate",
                    "confidence": 0.75,
                    "evidence_span": "花了两周才让一个健康检查正常工作",
                    "reasoning": "反语。字面是褒义,实际表达不满",
                }
            ]
        },
    ),
    # 4. 仅转述, 未表态 —— 必须排除出反仓库流程
    (
        "关于微服务,Martin Fowler 认为它适合复杂系统,而 Simon Brown 则认为大多数团队并不需要它。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "target": "microservices",
                    "polarity": "undetermined",
                    "strength": "weak",
                    "confidence": 0.9,
                    "evidence_span": "Martin Fowler 认为它适合复杂系统,而 Simon Brown 则认为大多数团队并不需要它",
                    "reasoning": "仅陈述他人观点,作者未表态",
                }
            ]
        },
    ),
    # 5. 中立
    (
        "微服务和单体各有优劣,选择取决于团队规模和系统复杂度。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "target": "microservices",
                    "polarity": "neutral",
                    "strength": "weak",
                    "confidence": 0.8,
                    "evidence_span": "各有优劣,选择取决于团队规模和系统复杂度",
                    "reasoning": "无倾向",
                }
            ]
        },
    ),
]

# JSON Schema, 约束模型输出结构
STANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "target": {"type": "string"},
                    "polarity": {"type": "string", "enum": ["pro", "con", "neutral", "undetermined"]},
                    "strength": {"type": "string", "enum": ["strong", "moderate", "weak"]},
                    "confidence": {"type": "number"},
                    "evidence_span": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["issue_id", "target", "polarity", "confidence", "evidence_span"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stances"],
    "additionalProperties": False,
}


def build_user_prompt(text: str, taxonomy: list[dict[str, str]]) -> str:
    """把议题清单和待分析文本组装成 user message"""
    return (
        "【议题清单】\n"
        + json.dumps(taxonomy, ensure_ascii=False, indent=2)
        + "\n\n【待分析文本】\n"
        + text
    )


# ---------------------------------------------------------------------------
# 2. 证据校验闸门 —— 最便宜的防幻觉手段
# ---------------------------------------------------------------------------
def normalize(s: str) -> str:
    """去掉所有空白字符, 避免因空格/换行导致误判"""
    return "".join(s.split())


def verify_evidence(text: str, span: str) -> bool:
    """证据必须是原文的精确子串(忽略空白差异)"""
    if not span or len(span.strip()) < 4:
        return False
    return normalize(span) in normalize(text)


def gate(polarity: str, confidence: float) -> str:
    """置信度三级分档"""
    if polarity == "undetermined":
        return "reject"  # 未表态 → 绝不用于反仓库
    if confidence < 0.6:
        return "reject"
    if confidence < 0.8:
        return "review"  # 可用于仓库, 不进反仓库
    return "accept"


# ---------------------------------------------------------------------------
# 3. 缓存
# ---------------------------------------------------------------------------
_CACHE: dict[str, list[dict[str, Any]]] = {}


def cache_key(text: str) -> str:
    raw = f"{text}::{TAXONOMY_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 4. LLM 调用 (用 OpenAI 兼容接口, 可换成 DeepSeek / 通义等)
# ---------------------------------------------------------------------------
def _call_llm(text: str, taxonomy: list[dict[str, str]]) -> dict[str, Any]:
    """
    真实调用。需要设置环境变量 OPENAI_API_KEY。
    若使用其他兼容接口, 改 base_url 即可。
    """
    from openai import OpenAI  # 延迟导入, 方便无 key 时跑 --selftest

    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for demo_text, demo_out in FEW_SHOT:
        messages.append({"role": "user", "content": build_user_prompt(demo_text, taxonomy)})
        messages.append({"role": "assistant", "content": json.dumps(demo_out, ensure_ascii=False)})
    messages.append({"role": "user", "content": build_user_prompt(text, taxonomy)})

    resp = client.chat.completions.create(
        model=os.environ.get("STANCE_MODEL", "gpt-4o-mini"),
        temperature=0,  # 判断任务, 必须确定性
        response_format={"type": "json_object"},
        messages=messages,
    )
    return json.loads(resp.choices[0].message.content)


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
MAX_RETRY = 2


@dataclass
class Stance:
    issue_id: str
    target: str
    polarity: str
    strength: str
    confidence: float
    evidence_span: str
    reasoning: str = ""
    verified: bool = False
    gate: str = "reject"


def extract(text: str, taxonomy: list[dict[str, str]] | None = None) -> list[Stance]:
    """
    从文本中抽取立场。
    带缓存 + 证据校验 + 重试。
    """
    taxonomy = taxonomy or ISSUE_TAXONOMY
    key = cache_key(text)
    if key in _CACHE:
        return [Stance(**s) for s in _CACHE[key]]

    results: list[Stance] = []
    for _ in range(MAX_RETRY + 1):
        raw = _call_llm(text, taxonomy)
        valid_ids = {t["issue_id"] for t in taxonomy}

        for item in raw.get("stances", []):
            # 过滤掉模型自创的议题
            if item.get("issue_id") not in valid_ids:
                continue
            ok = verify_evidence(text, item.get("evidence_span", ""))
            if not ok:
                continue  # 证据不在原文里 → 丢弃
            results.append(
                Stance(
                    issue_id=item["issue_id"],
                    target=item.get("target", ""),
                    polarity=item["polarity"],
                    strength=item.get("strength", "moderate"),
                    confidence=float(item["confidence"]),
                    evidence_span=item["evidence_span"],
                    reasoning=item.get("reasoning", ""),
                    verified=True,
                    gate=gate(item["polarity"], float(item["confidence"])),
                )
            )
        if results:
            break

    _CACHE[key] = [asdict(s) for s in results]
    return results


# ---------------------------------------------------------------------------
# 6. 长文本处理
# ---------------------------------------------------------------------------
def split_with_overlap(text: str, chunk_size: int = 3000, overlap: int = 200) -> list[str]:
    """按字符切块, 带重叠 —— 避免立场正好卡在分片边界上"""
    if len(text) <= chunk_size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def extract_long(text: str, taxonomy: list[dict[str, str]] | None = None) -> list[Stance]:
    """长文本: 分块抽取后合并。同议题多条取置信度最高的"""
    all_stances: list[Stance] = []
    for chunk in split_with_overlap(text):
        all_stances += extract(chunk, taxonomy)

    best: dict[str, Stance] = {}
    for s in all_stances:
        if s.issue_id not in best or s.confidence > best[s.issue_id].confidence:
            best[s.issue_id] = s
    return list(best.values())


# ---------------------------------------------------------------------------
# 7. 评测
# ---------------------------------------------------------------------------
def evaluate(gold_path: str) -> dict[str, float]:
    """
    评测集格式 (JSONL, 每行一条):
      {"text": "...", "expected_polarity": "con", "expected_issue": "microservices_vs_monolith"}

    返回三个指标:
      polarity_accuracy  —— 立场判准的比例
      evidence_pass_rate —— 证据校验通过率 (幻觉预警)
      issue_accuracy     —— 议题对齐准确率
    """
    total = pol_ok = issue_ok = evidence_ok = 0
    with open(gold_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            total += 1
            preds = extract(item["text"])
            if not preds:
                continue
            top = max(preds, key=lambda s: s.confidence)
            if top.issue_id == item["expected_issue"]:
                issue_ok += 1
            if top.polarity == item["expected_polarity"]:
                pol_ok += 1
            if top.verified:
                evidence_ok += 1

    return {
        "total": float(total),
        "polarity_accuracy": pol_ok / total if total else 0.0,
        "issue_accuracy": issue_ok / total if total else 0.0,
        "evidence_pass_rate": evidence_ok / total if total else 0.0,
    }


# ---------------------------------------------------------------------------
# 8. 自检(不需要 API key 也能跑, 验证校验闸门逻辑)
# ---------------------------------------------------------------------------
def _selftest() -> None:
    text = "我们去年上了微服务,结果排障时间翻了三倍。"
    print("normalize:", repr(normalize(" a b\n c ")))

    assert verify_evidence(text, "排障时间翻了三倍"), "应通过"
    assert not verify_evidence(text, "排障时间翻了十倍"), "改数字应拒绝"
    assert not verify_evidence(text, "我们非常后悔"), "编造应拒绝"
    print("证据校验闸门: 通过")

    assert gate("con", 0.9) == "accept"
    assert gate("con", 0.7) == "review"
    assert gate("con", 0.5) == "reject"
    assert gate("undetermined", 0.95) == "reject", "未表态必须拒绝"
    print("置信度分档: 通过")

    chunks = split_with_overlap("x" * 7000, chunk_size=3000, overlap=200)
    assert len(chunks) == 3, f"分块数不对: {len(chunks)}"
    print(f"长文本分块: 通过 ({len(chunks)} 块)")
    print("\n全部自检通过。设置 OPENAI_API_KEY 后可跑真实抽取。")


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="立场抽取")
    parser.add_argument("--text", help="直接分析一段文本")
    parser.add_argument("--file", help="从 txt 文件读取文本")
    parser.add_argument("--eval", dest="eval_path", help="跑评测集 (JSONL)")
    parser.add_argument("--selftest", action="store_true", help="只跑自检, 不调用 API")
    args = parser.parse_args()

    if args.selftest or not any([args.text, args.file, args.eval_path]):
        _selftest()
        return

    if args.eval_path:
        metrics = evaluate(args.eval_path)
        print("评测结果")
        print("-" * 32)
        print(f"样本数           {int(metrics['total'])}")
        print(f"立场准确率       {metrics['polarity_accuracy']:.1%}   (目标 >80%)")
        print(f"议题对齐准确率   {metrics['issue_accuracy']:.1%}   (目标 >90%)")
        print(f"证据校验通过率   {metrics['evidence_pass_rate']:.1%}   (目标 >95%, 低于则模型在编证据)")
        return

    text = args.text
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()

    stances = extract_long(text)
    if not stances:
        print("未抽到任何立场(可能是议题不匹配, 或证据校验未通过)")
        return

    print(f"抽到 {len(stances)} 条立场\n")
    for s in stances:
        print(f"议题      {s.issue_id}")
        print(f"立场      {s.polarity} ({s.strength})  置信度 {s.confidence:.2f}")
        print(f"处理      {s.gate}")
        print(f"证据      {s.evidence_span}")
        if s.reasoning:
            print(f"理由      {s.reasoning}")
        print("-" * 40)


if __name__ == "__main__":
    _cli()
