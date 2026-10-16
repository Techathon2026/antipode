"""
立场抽取模块 (Stance Extraction) v3
=====================================
输入: 一段文本
输出: 该文本对预定义议题的立场 + 原文证据

【核心数据约定 —— 全系统最重要的一条】
每个议题都有两个明确的对立方 side_a / side_b。
模型不输出抽象的 pro/con, 而是直接输出 "文本支持的是哪一方"。

    ✅ {"issue_id": "react_vs_vue", "supports": "Vue"}       —— 文本支持 Vue
    ✅ {"issue_id": "react_vs_vue", "supports": "React"}     —— 文本支持 React
    ✅ {"issue_id": "react_vs_vue", "supports": "neutral"}   —— 中立讨论
    ✅ {"issue_id": "react_vs_vue", "supports": "undetermined"} —— 只转述别人, 没表态

为什么不用 pro/con:
    "支持微服务" 用 pro 表示、"反对单体" 用 con 表示 —— 同一句话有两种合法写法,
    反仓库就无法判断"这条信息的对立面是谁"。直接标"支持哪一方"则唯一确定。

反仓库取对立面只需一行:
    opposite = issue["side_b"] if stance.side == "a" else issue["side_a"]

设计要点:
  1. 议题必须来自预定义清单 (不许自创), 保证每个议题都有确定的二元结构
  2. 强制模型输出 evidence_span, 用代码校验它是否真的是原文子串 → 防幻觉
  3. 置信度三级分档: accept / review / reject (反仓库门槛比仓库更高)
  4. 长文本分块 + 重叠 + 合并
  5. 结果按内容哈希缓存
  6. 统计"幻觉指标": 自创议题数 / 证据校验失败数
  7. 没有 API key 也能用 --mock 跑通全流程 (演示兜底 + 本地联调)

依赖: pip install -r requirements.txt
用法:
  python stance_extractor.py --selftest        # 不联网, 验证校验逻辑
  python stance_extractor.py --mock            # 不联网, 假模型跑通全流程
  python stance_extractor.py --taxonomy        # 打印议题清单
  python stance_extractor.py --text "……"      # 真实抽取
  python stance_extractor.py --eval gold_set.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Any

# ---------------------------------------------------------------------------
# 0. 加载 .env —— 固定读本文件同目录下的 .env, 不受工作目录影响
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_HERE, ".env"))
except ImportError:
    _env_path = os.path.join(_HERE, ".env")
    if os.path.exists(_env_path):
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# 1. 议题清单 —— 唯一需要人工维护的东西
# ---------------------------------------------------------------------------
# 规则:
#   · 只列"有真实争论"的议题, 每个议题必须能明确说出正反两方是什么
#   · issue_id 一旦定下就不要改, 它是全系统的外键
#   · side_a / side_b 就是这场争论的两个对立面, 顺序无所谓, 但必须互斥且穷尽
#   · keywords 供离线 mock 与后续检索阶段使用
#   · 严禁加入政治、宗教、地域类议题 (黑客松现场审核零风险)
ISSUE_TAXONOMY: list[dict[str, Any]] = [
    {
        "issue_id": "microservices_vs_monolith",
        "label": "微服务 vs 单体架构",
        "domain": "软件架构",
        "side_a": "微服务",
        "side_b": "单体",
        "keywords": ["微服务", "单体", "monolith", "microservice", "服务拆分"],
    },
    {
        "issue_id": "tdd_necessity",
        "label": "测试驱动开发是否必要",
        "domain": "工程实践",
        "side_a": "先写测试",
        "side_b": "先写实现",
        "keywords": ["TDD", "测试驱动", "测试先行", "先写测试", "单元测试"],
    },
    {
        "issue_id": "monorepo_vs_multirepo",
        "label": "单仓 vs 多仓",
        "domain": "工程实践",
        "side_a": "单体仓库",
        "side_b": "多仓库",
        "keywords": ["monorepo", "单仓", "单体仓库", "多仓库", "多仓"],
    },
    {
        "issue_id": "react_vs_vue",
        "label": "React vs Vue",
        "domain": "前端",
        "side_a": "React",
        "side_b": "Vue",
        "keywords": ["React", "Vue", "hooks", "组合式 API"],
    },
    {
        "issue_id": "rest_vs_graphql",
        "label": "REST vs GraphQL",
        "domain": "接口设计",
        "side_a": "REST",
        "side_b": "GraphQL",
        "keywords": ["REST", "GraphQL", "schema", "接口设计"],
    },
    {
        "issue_id": "orm_or_raw_sql",
        "label": "ORM 还是手写 SQL",
        "domain": "数据访问",
        "side_a": "ORM",
        "side_b": "手写 SQL",
        "keywords": ["ORM", "手写 SQL", "原生 SQL", "查询构造"],
    },
    {
        "issue_id": "typescript_strictness",
        "label": "TypeScript 严格模式是否值得",
        "domain": "前端",
        "side_a": "开严格模式",
        "side_b": "关严格模式",
        "keywords": ["TypeScript", "严格模式", "strict", "类型标注", "any"],
    },
    {
        "issue_id": "oop_vs_fp",
        "label": "面向对象 vs 函数式",
        "domain": "编程范式",
        "side_a": "面向对象",
        "side_b": "函数式",
        "keywords": ["面向对象", "OOP", "函数式", "FP", "纯函数", "继承"],
    },
    {
        "issue_id": "ai_assistant_weakens_dev",
        "label": "AI 编程助手是否让程序员退化",
        "domain": "工程实践",
        "side_a": "AI 助手有害",
        "side_b": "AI 助手有益",
        "keywords": ["Copilot", "AI 编程", "代码助手", "AI 写代码", "自动补全"],
    },
    {
        "issue_id": "remote_vs_onsite",
        "label": "远程办公 vs 坐班",
        "domain": "职场",
        "side_a": "远程办公",
        "side_b": "坐班",
        "keywords": ["远程办公", "坐班", "回办公室", "WFH", "混合办公"],
    },
    {
        "issue_id": "rust_vs_cpp",
        "label": "Rust 还是 C++",
        "domain": "编程语言",
        "side_a": "Rust",
        "side_b": "C++",
        "keywords": ["Rust", "C++", "内存安全", "所有权"],
    },
    {
        "issue_id": "cloud_vs_selfhost",
        "label": "上云 vs 自建",
        "domain": "基础设施",
        "side_a": "上云",
        "side_b": "自建",
        "keywords": ["上云", "自建", "私有部署", "云厂商", "IDC", "裸金属"],
    },
    {
        "issue_id": "code_review_strictness",
        "label": "严格 Code Review 是否值得",
        "domain": "团队协作",
        "side_a": "严格评审",
        "side_b": "宽松评审",
        "keywords": ["Code Review", "代码评审", "评审", "合并请求"],
    },
    {
        "issue_id": "static_vs_dynamic_typing",
        "label": "静态类型 vs 动态类型",
        "domain": "编程语言",
        "side_a": "静态类型",
        "side_b": "动态类型",
        "keywords": ["静态类型", "动态类型", "类型注解", "类型系统"],
    },
    {
        "issue_id": "tabs_vs_spaces",
        "label": "缩进用 Tab 还是空格",
        "domain": "工程习惯",
        "side_a": "Tab",
        "side_b": "空格",
        "keywords": ["Tab", "缩进", "制表符"],
    },
    {
        "issue_id": "tofu_pudding",
        "label": "甜豆腐脑 vs 咸豆腐脑",
        "domain": "生活偏好",
        "side_a": "甜豆腐脑",
        "side_b": "咸豆腐脑",
        "keywords": ["豆腐脑"],
    },
]

TAXONOMY_VERSION = "v3"  # 清单、prompt 或数据约定变更时递增, 用于缓存失效

_ISSUE_INDEX: dict[str, dict[str, Any]] = {t["issue_id"]: t for t in ISSUE_TAXONOMY}

NEUTRAL = "neutral"
UNDETERMINED = "undetermined"

# ---------------------------------------------------------------------------
# 2. Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是立场分析器。给定一段文本和一份议题清单, 判断文本对这些议题的立场。

【硬性规则】
1. issue_id 必须来自给定清单, 不许自创。一个都不匹配就返回空数组。
2. supports 必须**原样**填写清单里该议题的 side_a 或 side_b 文本, 或者 "neutral" / "undetermined"。
3. 立场是文本【实际表达】的立场, 不是作者的自我声明。
4. evidence_span 必须是原文的【精确子串】, 一个字都不能改。做不到就降低 confidence。
5. 讽刺、反话、阴阳怪气, 按【实际含义】判断, 不要按字面意思。
6. 只是【引用】或【转述】他人观点、自己没表态的, supports 填 "undetermined"。
7. 中立地讨论正反两面、不倾向任何一方, supports 填 "neutral"。
8. 注意【隐含立场】: 没直说支持谁, 但字里行间态度明确的, 要判出来。
9. 只是提到某个词、没表达立场的 (讲历史、列举名词、背景介绍), 不要输出。

【判断 supports 的方向】
- 文本夸 A 骂 B  → supports = 清单里 A 那一侧
- 文本说"我选了 A"、"我们用 A" → supports = A
- 文本罗列的全是 A 的坏处 (即使没直说反对) → supports = A 的对立面
- 两边各打五十大板 → "neutral"

【confidence】0 到 1。证据不充分时必须给低分, 不要硬编。

只输出 JSON, 格式为 {"stances": [...]}, 不要任何其他文字。"""

# Few-shot 示例, 覆盖 7 种最难的情况
FEW_SHOT: list[tuple[str, dict[str, Any]]] = [
    # 1. 直接支持
    (
        "我们把单体拆成微服务之后, 部署频率从每月一次提升到每天十次, 团队可以独立发布, 这是巨大的收益。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "supports": "微服务",
                    "strength": "strong",
                    "confidence": 0.95,
                    "evidence_span": "部署频率从每月一次提升到每天十次",
                    "reasoning": "明确陈述收益",
                }
            ]
        },
    ),
    # 2. 隐含反对 (最难的一类): 一个"反对"字样都没有
    (
        "我们去年上了微服务, 结果光是链路追踪就搭了三个月, 一个下单请求要跨七个服务, 排障时间翻了三倍。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "supports": "单体",
                    "strength": "strong",
                    "confidence": 0.85,
                    "evidence_span": "一个下单请求要跨七个服务, 排障时间翻了三倍",
                    "reasoning": "未直接表态, 但罗列的全是负面后果, 隐含反对微服务",
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
                    "supports": "单体",
                    "strength": "moderate",
                    "confidence": 0.75,
                    "evidence_span": "花了两周才让一个健康检查正常工作",
                    "reasoning": "反语。字面是褒义, 实际表达不满",
                }
            ]
        },
    ),
    # 4. 仅转述, 未表态 —— 必须排除出反仓库流程
    (
        "关于微服务, Martin Fowler 认为它适合复杂系统, 而 Simon Brown 则认为大多数团队并不需要它。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "supports": "undetermined",
                    "strength": "weak",
                    "confidence": 0.9,
                    "evidence_span": "Martin Fowler 认为它适合复杂系统, 而 Simon Brown 则认为大多数团队并不需要它",
                    "reasoning": "仅陈述他人观点, 作者未表态",
                }
            ]
        },
    ),
    # 5. 中立
    (
        "微服务和单体各有优劣, 选择取决于团队规模和系统复杂度。",
        {
            "stances": [
                {
                    "issue_id": "microservices_vs_monolith",
                    "supports": "neutral",
                    "strength": "weak",
                    "confidence": 0.8,
                    "evidence_span": "各有优劣, 选择取决于团队规模和系统复杂度",
                    "reasoning": "无倾向",
                }
            ]
        },
    ),
    # 6. 多议题
    (
        "我们全用 Vue, 但 TypeScript 的 strict 模式一开始真的很痛苦。",
        {
            "stances": [
                {
                    "issue_id": "react_vs_vue",
                    "supports": "Vue",
                    "strength": "moderate",
                    "confidence": 0.8,
                    "evidence_span": "我们全用 Vue",
                    "reasoning": "明确选择 Vue",
                },
                {
                    "issue_id": "typescript_strictness",
                    "supports": "关严格模式",
                    "strength": "moderate",
                    "confidence": 0.72,
                    "evidence_span": "TypeScript 的 strict 模式一开始真的很痛苦",
                    "reasoning": "对严格模式表达负面体验",
                },
            ]
        },
    ),
    # 7. 只提到名词、没有立场 —— 必须不输出
    (
        "本周技术分享会的主题包括微服务、GraphQL 和 Kubernetes 的基础介绍。",
        {"stances": []},
    ),
]

# JSON Schema, 文档用; 实际调用走 json_object 以兼容更多网关
STANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "supports": {"type": "string"},
                    "strength": {"type": "string", "enum": ["strong", "moderate", "weak"]},
                    "confidence": {"type": "number"},
                    "evidence_span": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["issue_id", "supports", "confidence", "evidence_span"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stances"],
    "additionalProperties": False,
}


def build_user_prompt(text: str, taxonomy: list[dict[str, Any]]) -> str:
    """把议题清单和待分析文本组装成 user message"""
    slim = [
        {"issue_id": t["issue_id"], "label": t["label"], "side_a": t["side_a"], "side_b": t["side_b"]}
        for t in taxonomy
    ]
    return (
        "【议题清单】\n"
        + json.dumps(slim, ensure_ascii=False, indent=2)
        + "\n\n【待分析文本】\n"
        + text
    )


# ---------------------------------------------------------------------------
# 3. 把 supports 归一成 side 键 —— 反仓库取对立面全靠它
# ---------------------------------------------------------------------------
def resolve_side(issue: dict[str, Any], supports: str) -> str:
    """把模型给的 supports 文本归一为 'a' / 'b' / 'neutral' / 'undetermined' / 'unknown'"""
    s = (supports or "").strip()
    if not s:
        return "unknown"
    low = s.lower()
    if low in (NEUTRAL, "中立", "neutral"):
        return NEUTRAL
    if low in (UNDETERMINED, "未表态", "无法确定"):
        return UNDETERMINED

    for key, tag in (("side_a", "a"), ("side_b", "b")):
        name = str(issue.get(key, "")).strip()
        if not name:
            continue
        if s == name or low == name.lower():
            return tag
    # 容错: 包含关系(模型多写了几个字)
    for key, tag in (("side_a", "a"), ("side_b", "b")):
        name = str(issue.get(key, "")).strip()
        if name and (name in s or (len(low) >= 2 and low in name.lower())):
            return tag
    return "unknown"


def opposite_side(issue: dict[str, Any], side: str) -> str | None:
    """反仓库取对立面。只有明确站队的条目才有对立面。"""
    if side == "a":
        return issue["side_b"]
    if side == "b":
        return issue["side_a"]
    return None


# ---------------------------------------------------------------------------
# 4. 证据校验闸门 —— 最便宜的防幻觉手段
# ---------------------------------------------------------------------------
def normalize(s: str) -> str:
    """去掉所有空白字符与常见标点, 避免因空格/换行/中英标点导致误判"""
    s = re.sub(r"[\s\u200b]+", "", s)
    s = re.sub(r"[，。！？；：、,.!?;:\"'“”‘’（）()\[\]【】—-]+", "", s)
    return s


def verify_evidence(text: str, span: str) -> bool:
    """证据必须是原文的精确子串(忽略空白与标点差异)"""
    if not span or len(span.strip()) < 4:
        return False
    return normalize(span) in normalize(text)


def gate(side: str, confidence: float) -> str:
    """置信度三级分档"""
    if side in (NEUTRAL, UNDETERMINED, "unknown"):
        return "reject"  # 未站队 → 绝不用于反仓库
    if confidence < 0.6:
        return "reject"
    if confidence < 0.8:
        return "review"  # 可用于仓库, 不进反仓库
    return "accept"


# ---------------------------------------------------------------------------
# 5. 统计结构 —— 幻觉指标就靠它
# ---------------------------------------------------------------------------
@dataclass
class ExtractStats:
    attempts: int = 0          # 实际调用了模型几次
    raw_items: int = 0         # 模型返回的原始条数
    invented_issue: int = 0    # 自创议题(不在清单里)被丢弃的条数
    evidence_failed: int = 0   # 证据不在原文里被丢弃的条数
    verified: int = 0          # 通过全部校验的条数
    from_cache: bool = False   # 本次是否命中缓存


def evidence_pass_rate(s: ExtractStats) -> float:
    """证据校验通过率 = 通过 / (原始条数 - 自创议题条数)

    只关心"议题合法"的那些条里有多少证据是真的。这是幻觉预警灯, 目标 > 95%。
    """
    checked = s.raw_items - s.invented_issue
    return (s.verified / checked) if checked > 0 else 1.0


def _accumulate(agg: ExtractStats, one: ExtractStats) -> None:
    agg.attempts += one.attempts
    agg.raw_items += one.raw_items
    agg.invented_issue += one.invented_issue
    agg.evidence_failed += one.evidence_failed
    agg.verified += one.verified


# ---------------------------------------------------------------------------
# 6. 缓存
# ---------------------------------------------------------------------------
_CACHE: dict[str, dict[str, Any]] = {}


def cache_key(text: str) -> str:
    raw = f"{text}::{TAXONOMY_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 7. LLM 调用 (OpenAI 兼容接口, 可换 DeepSeek / GLM / 通义等)
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    pass


def _is_format_error(err: Exception) -> bool:
    msg = str(err).lower()
    signs = ["response_format", "json_object", "json mode", "unsupported", "invalid_request", "400"]
    return any(s in msg for s in signs)


def _parse_json(content: str) -> dict[str, Any]:
    """容错解析: 去掉 ``` 包裹, 必要时截取第一个 { 到最后一个 }"""
    if not content:
        raise LLMError("模型返回空内容")
    s = content.strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i : j + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"无法解析模型输出为 JSON: {e}\n原始内容: {s[:300]}") from e
    raise LLMError(f"模型输出里找不到 JSON:\n{s[:300]}")


def _build_messages(text: str, taxonomy: list[dict[str, Any]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for demo_text, demo_out in FEW_SHOT:
        messages.append({"role": "user", "content": build_user_prompt(demo_text, taxonomy)})
        messages.append({"role": "assistant", "content": json.dumps(demo_out, ensure_ascii=False)})
    messages.append({"role": "user", "content": build_user_prompt(text, taxonomy)})
    return messages


def _call_llm(text: str, taxonomy: list[dict[str, Any]]) -> dict[str, Any]:
    """真实调用。需要 OPENAI_API_KEY; 地址取 OPENAI_BASE_URL。"""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not api_key:
        raise LLMError("没有找到 OPENAI_API_KEY。请先在 code/.env 里填好密钥, 或改用 --mock 离线模式。")
    if not re.match(r"^https?://", base_url):
        raise LLMError(
            f"OPENAI_BASE_URL 还不是一个合法地址: {base_url!r}\n请先在 code/.env 里填好真实接口地址。"
        )

    from openai import OpenAI  # 延迟导入, 方便无 key 时跑 --selftest / --mock

    client = OpenAI(api_key=api_key, base_url=base_url)
    model = os.environ.get("STANCE_MODEL", "deepseek-flash")
    kwargs: dict[str, Any] = dict(
        model=model, temperature=0, messages=_build_messages(text, taxonomy), max_tokens=2048
    )

    try:
        resp = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception as e:  # noqa: BLE001 —— 网关差异太大, 统一降级处理
        if not _is_format_error(e):
            raise
        resp = client.chat.completions.create(**kwargs)  # 该网关不支持 json_object

    return _parse_json(resp.choices[0].message.content or "")


# ---------------------------------------------------------------------------
# 7b. 离线假模型 —— 没有 key 也能跑通全流程
# ---------------------------------------------------------------------------
_NEG_CUES = ["结果", "翻倍", "三倍", "崩溃", "后悔", "失败", "拖垮", "浪费",
             "扛不住", "痛苦", "坑", "排着", "不值", "难受", "别碰", "纯属", "慢",
             "离谱", "没法", "太嫩", "不考虑", "还不如", "太过了", "啰嗦", "退化"]
_POS_CUES = ["提升", "收益", "很好", "稳定", "更快", "提高", "顺畅", "直观",
             "底气", "拦掉", "喜欢", "推荐", "值得", "对就", "省下", "省了",
             "踏实", "效率", "坚持", "当然要", "好用", "兜底", "更好"]
_NEUTRAL_CUES = ["各有优劣", "没有定论", "取决于", "不评价", "都行", "一半一半",
                 "好坏参半", "都挺好", "看团队习惯", "主要看"]
# 转述他人观点: 必须"第三方主语 + 观点动词", 避免把"我觉得"误判成转述
_UNDET_RE = re.compile(r"(他|她|有人|作者|网友|专家|业内|Martin\w*|Simon\w*)(认为|觉得|表示|指出|主张)")


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？；!?;\n]+", text)
    return [p for p in (x.strip() for x in parts) if p]


def _mock_llm(text: str, taxonomy: list[dict[str, Any]]) -> dict[str, Any]:
    """规则版假模型。只为在没有 API 时验证流水线, 不是真正的抽取, 准确率无参考价值。"""
    sents = _split_sentences(text)
    stances: list[dict[str, Any]] = []

    for item in taxonomy:
        hits = [k for k in item.get("keywords", []) if k and k in text]
        if not hits:
            continue
        sent = next((s for s in sents if any(h in s for h in hits)), sents[0] if sents else text)

        if any(c in text for c in _NEUTRAL_CUES):
            supports, conf = NEUTRAL, 0.80
        elif _UNDET_RE.search(text) and not any(c in text for c in _NEG_CUES + _POS_CUES):
            supports, conf = UNDETERMINED, 0.85
        elif any(c in text for c in _NEG_CUES):
            supports, conf = item["side_b"], 0.85
        elif any(c in text for c in _POS_CUES):
            supports, conf = item["side_a"], 0.85
        else:
            continue  # 只是提到了词, 没有态度 → 不输出

        stances.append(
            {
                "issue_id": item["issue_id"],
                "supports": supports,
                "strength": "moderate",
                "confidence": conf,
                "evidence_span": sent,
                "reasoning": "[mock] 规则命中: " + ",".join(hits),
            }
        )
    return {"stances": stances}


# ---------------------------------------------------------------------------
# 8. 主流程
# ---------------------------------------------------------------------------
MAX_RETRY = 1  # 只在"疑似幻觉"时多试一次

_USE_MOCK = False


@dataclass
class Stance:
    issue_id: str
    supports: str          # 模型输出: 文本支持的那一方(原文文本)
    side: str              # 归一后的键: "a" / "b" / "neutral" / "undetermined" / "unknown"
    strength: str
    confidence: float
    evidence_span: str
    reasoning: str = ""
    verified: bool = False
    gate: str = "reject"

    @property
    def opposite(self) -> str | None:
        """反仓库要放的对立面。未站队则没有对立面。"""
        issue = _ISSUE_INDEX.get(self.issue_id)
        return opposite_side(issue, self.side) if issue else None


def _extract_core(
    text: str, taxonomy: list[dict[str, Any]] | None = None
) -> tuple[list[Stance], ExtractStats]:
    """核心抽取, 返回 (立场列表, 本次统计)"""
    taxonomy = taxonomy or ISSUE_TAXONOMY
    stats = ExtractStats()

    key = cache_key(text)
    if key in _CACHE:
        blob = _CACHE[key]
        stats = ExtractStats(**blob["stats"])
        stats.from_cache = True
        return [Stance(**s) for s in blob["stances"]], stats

    idx = {t["issue_id"]: t for t in taxonomy}
    results: list[Stance] = []

    for _ in range(MAX_RETRY + 1):
        stats.attempts += 1
        raw = _mock_llm(text, taxonomy) if _USE_MOCK else _call_llm(text, taxonomy)
        items = raw.get("stances") or []

        if not items:
            # 模型明确判断"无立场" → 合法结论, 不重试 (重试只会白烧钱)
            break

        for item in items:
            stats.raw_items += 1
            issue = idx.get(item.get("issue_id"))
            if issue is None:
                stats.invented_issue += 1
                continue  # 模型自创议题 → 丢弃
            span = item.get("evidence_span", "")
            if not verify_evidence(text, span):
                stats.evidence_failed += 1
                continue  # 证据不在原文里 → 丢弃
            side = resolve_side(issue, item.get("supports", ""))
            conf = float(item.get("confidence", 0.0))
            stats.verified += 1
            results.append(
                Stance(
                    issue_id=issue["issue_id"],
                    supports=item.get("supports", ""),
                    side=side,
                    strength=item.get("strength", "moderate"),
                    confidence=conf,
                    evidence_span=span,
                    reasoning=item.get("reasoning", ""),
                    verified=True,
                    gate=gate(side, conf),
                )
            )

        if results:
            break
        # 有原始输出但全部被丢弃 → 疑似幻觉, 值得重试一次

    # 同一议题多条只留置信度最高的
    best: dict[str, Stance] = {}
    for s in results:
        if s.issue_id not in best or s.confidence > best[s.issue_id].confidence:
            best[s.issue_id] = s
    results = list(best.values())

    _CACHE[key] = {
        "stances": [asdict(s) for s in results],
        "stats": {k: v for k, v in asdict(stats).items() if k != "from_cache"},
    }
    return results, stats


def extract(text: str, taxonomy: list[dict[str, Any]] | None = None) -> list[Stance]:
    """从文本中抽取立场(对外主接口)"""
    return _extract_core(text, taxonomy)[0]


# ---------------------------------------------------------------------------
# 9. 长文本处理
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


def extract_long(text: str, taxonomy: list[dict[str, Any]] | None = None) -> list[Stance]:
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
# 10. 评测
# ---------------------------------------------------------------------------
def evaluate(gold_path: str) -> dict[str, Any]:
    """
    评测集格式 (JSONL, 每行一条):
      {"text": "...", "issue": "react_vs_vue", "side": "b"}

    side 取值: "a" | "b" | "neutral" | "undetermined"

    返回指标:
      side_accuracy      —— 立场判准的比例 (核心指标)
      issue_accuracy     —— 议题对齐准确率
      evidence_pass_rate —— 证据校验通过率 (幻觉预警, <95% 说明模型在编证据)
      invented_issue_rate—— 自创议题率 (应为 0)
    """
    total = side_ok = issue_ok = 0
    agg = ExtractStats()
    failures: list[str] = []

    with open(gold_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            total += 1
            preds, st = _extract_core(item["text"])
            _accumulate(agg, st)

            exp_issue, exp_side = item["issue"], item["side"]

            if not preds:
                failures.append(f"L{lineno} 未抽到立场 | 期望 {exp_side}/{exp_issue}")
                continue

            top = max(preds, key=lambda s: s.confidence)
            got_issue = top.issue_id == exp_issue
            got_side = got_issue and top.side == exp_side
            issue_ok += int(got_issue)
            side_ok += int(got_side)

            if not (got_issue and got_side):
                failures.append(
                    f"L{lineno} 期望 {exp_side}/{exp_issue} | 实得 {top.side}({top.supports})/{top.issue_id}"
                    f" | 文本: {item['text'][:36]}"
                )

    return {
        "total": float(total),
        "side_accuracy": side_ok / total if total else 0.0,
        "issue_accuracy": issue_ok / total if total else 0.0,
        "evidence_pass_rate": evidence_pass_rate(agg),
        "invented_issue_rate": (agg.invented_issue / agg.raw_items) if agg.raw_items else 0.0,
        "raw_items": float(agg.raw_items),
        "api_calls": float(agg.attempts),
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# 11. 自检(不需要 API key)
# ---------------------------------------------------------------------------
def _selftest() -> None:
    text = "我们去年上了微服务, 结果排障时间翻了三倍。"

    assert normalize(" a b\n c ") == "abc", normalize(" a b\n c ")
    assert normalize("排障, 时间。") == "排障时间", normalize("排障, 时间。")
    print("① 文本归一化(含标点): 通过")

    assert verify_evidence(text, "排障时间翻了三倍"), "应通过"
    assert verify_evidence(text, "排障时间翻了三倍。"), "带句号也应通过(标点已归一)"
    assert not verify_evidence(text, "排障时间翻了十倍"), "改数字应拒绝"
    assert not verify_evidence(text, "我们非常后悔"), "编造应拒绝"
    assert not verify_evidence(text, "翻倍"), "过短应拒绝"
    print("② 证据校验闸门: 通过")

    ms = _ISSUE_INDEX["microservices_vs_monolith"]
    assert resolve_side(ms, "微服务") == "a"
    assert resolve_side(ms, "单体") == "b"
    assert resolve_side(ms, "我觉得是单体") == "b", "应支持包含式容错"
    assert resolve_side(ms, "neutral") == NEUTRAL
    assert resolve_side(ms, "undetermined") == UNDETERMINED
    assert resolve_side(ms, "不知道") == "unknown"
    assert opposite_side(ms, "a") == "单体", "反仓库要取对立面"
    assert opposite_side(ms, "b") == "微服务"
    assert opposite_side(ms, NEUTRAL) is None, "中立没有对立面"
    print("③ 立场归一化与对立面推导: 通过")

    assert gate("a", 0.9) == "accept"
    assert gate("a", 0.7) == "review"
    assert gate("a", 0.5) == "reject"
    assert gate(UNDETERMINED, 0.95) == "reject", "未表态必须拒绝"
    assert gate(NEUTRAL, 0.95) == "reject", "中立必须拒绝"
    print("④ 置信度分档: 通过")

    chunks = split_with_overlap("x" * 7000, chunk_size=3000, overlap=200)
    assert len(chunks) == 3, f"分块数不对: {len(chunks)}"
    print(f"⑤ 长文本分块: 通过 ({len(chunks)} 块)")

    # 统计口径自检: 5 条原始, 1 条自创议题, 1 条证据失败, 3 条通过 → 通过率 3/4
    s = ExtractStats(raw_items=5, invented_issue=1, evidence_failed=1, verified=3)
    assert abs(evidence_pass_rate(s) - 0.75) < 1e-9, evidence_pass_rate(s)
    assert evidence_pass_rate(ExtractStats(raw_items=0)) == 1.0, "没有原始输出时不应判为幻觉"
    print("⑥ 幻觉指标口径: 通过")

    # 离线假模型端到端
    global _USE_MOCK
    _USE_MOCK = True
    _CACHE.clear()
    preds = extract("我们去年上了微服务, 结果排障时间翻了三倍, 运维被拖垮了。")
    assert preds and preds[0].issue_id == "microservices_vs_monolith", preds
    assert preds[0].side == "b", preds[0].side
    assert preds[0].verified and preds[0].opposite == "微服务", preds[0]
    print(f"⑦ 离线假模型端到端: 通过 ({preds[0].supports} / 对立面 {preds[0].opposite})")
    _USE_MOCK = False
    _CACHE.clear()

    print("\n全部自检通过。")
    print("  下一步: python stance_extractor.py --mock            (离线跑通全流程)")
    print("          python stance_extractor.py --text \"…\"        (接真实模型)")


# ---------------------------------------------------------------------------
# 12. 命令行
# ---------------------------------------------------------------------------
def _print_stances(stances: list[Stance]) -> None:
    if not stances:
        print("未抽到任何立场(可能是议题不匹配, 或证据校验未通过)")
        return
    print(f"抽到 {len(stances)} 条立场\n")
    for s in stances:
        opp = s.opposite
        print(f"议题      {s.issue_id}")
        print(f"支持      {s.supports}  [side={s.side}]  强度 {s.strength}  置信度 {s.confidence:.2f}")
        print(f"处理      {s.gate}" + (f"   → 反仓库对立面: {opp}" if opp and s.gate == "accept" else ""))
        print(f"证据      {s.evidence_span}")
        if s.reasoning:
            print(f"理由      {s.reasoning}")
        print("-" * 46)


def _cli() -> None:
    import argparse

    global _USE_MOCK

    parser = argparse.ArgumentParser(description="立场抽取 (stance extraction)")
    parser.add_argument("--text", help="直接分析一段文本")
    parser.add_argument("--file", help="从 txt 文件读取文本")
    parser.add_argument("--eval", dest="eval_path", help="跑评测集 (JSONL)")
    parser.add_argument("--out", help="把结果写成 JSON 文件")
    parser.add_argument("--taxonomy", action="store_true", help="打印议题清单后退出")
    parser.add_argument("--mock", action="store_true", help="离线模式, 用规则假模型跑通流程")
    parser.add_argument("--selftest", action="store_true", help="只跑自检, 不调用 API")
    args = parser.parse_args()

    if args.taxonomy:
        print(json.dumps(ISSUE_TAXONOMY, ensure_ascii=False, indent=2))
        return

    if args.selftest or not any([args.text, args.file, args.eval_path, args.mock]):
        _selftest()
        return

    _USE_MOCK = args.mock

    if args.eval_path:
        metrics = evaluate(args.eval_path)
        print("评测结果")
        print("-" * 50)
        if _USE_MOCK:
            print("⚠️  当前是【离线假模型】(--mock), 下面这些数字没有参考价值。")
            print("    它只是规则桩, 用来验证流水线是否跑通。真正评测请去掉 --mock。")
            print("-" * 50)
        print(f"样本数             {int(metrics['total'])}")
        print(f"模型调用次数       {int(metrics['api_calls'])}")
        print(f"立场准确率         {metrics['side_accuracy']:.1%}   (目标 > 80%)")
        print(f"议题对齐准确率     {metrics['issue_accuracy']:.1%}   (目标 > 90%)")
        print(f"证据校验通过率     {metrics['evidence_pass_rate']:.1%}   (目标 > 95%, 低于说明模型在编证据)")
        print(f"自创议题率         {metrics['invented_issue_rate']:.1%}   (目标 0%)")
        if metrics["failures"]:
            print("\n未通过样本:")
            for line in metrics["failures"][:25]:
                print("  " + line)
        return

    text = args.text
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
    if text is None:
        _selftest()
        return

    stances = extract_long(text)
    _print_stances(stances)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "text": text,
                    "mock": _USE_MOCK,
                    "stances": [asdict(s) for s in stances],
                    "opposite_of": {s.issue_id: s.opposite for s in stances if s.gate == "accept"},
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    _cli()
