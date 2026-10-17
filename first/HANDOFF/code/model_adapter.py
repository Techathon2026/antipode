#!/usr/bin/env python3
"""模型适配层 —— 把"判立场"这个需求和"具体用哪个模型"解耦。

【为什么需要这个文件】
反引擎只有一个地方真正需要模型能力：**判断一段文本支持议题的哪一方**。
至于这个能力由谁提供 —— 免费的 GLM、比赛方给的 DeepSeek、还是队友自己训练的模型 ——
不应该影响流水线其它部分。所以这里做一层适配。

三种后端：

    openai_compat   走 OpenAI 兼容 HTTP 接口（现在用 GLM 免费档，改 .env 即可）
    batch_jsonl     ★ 读预先算好的标签文件 —— **队友训的模型推荐走这条**
    local_file      直接加载本地模型文件（需要队友提供一个调用样例）

【★ 为什么推荐 batch_jsonl（离线批处理）】
队友的模型是一个**模型文件**，不是服务。现场演示时如果每次都要加载权重，
会有一两分钟的空白 —— 评委巡场时这是致命的。所以推荐：

    队友写个脚本：输入 JSONL（一行一条候选文本）→ 输出 JSONL（一行一条标签）
    我在流水线里读这个结果文件，**零加载延迟、现场不会崩、队友不在场也能演示**

这样两边完全解耦：他的模型怎么训、用什么框架，我都不用管。

【⚠️ 一个必须知道的错配：分类模型给不出证据】
本系统的立场抽取输出是结构化的：
    {issue_id, supports, strength, confidence, evidence_span, reasoning}
其中 `evidence_span`（原文里支撑该判断的那句话）是**证据闸门的输入**，
没有它就只能丢弃这条 —— 而分类模型只能给"标签 + 概率"，给不出证据片段。

所以：
    · 分类模型可以当**门**：先筛掉不沾题的、明确中立的，减少后续模型调用
    · 但**完整抽取仍需要生成式模型**（哪怕只是让它把证据句子摘出来）
    · 除非队友的模型是多任务输出（标签 + 片段），那才能直接替换

用法：
    # 离线自检（不联网）
    python model_adapter.py --selftest

    # 用 OpenAI 兼容后端判一条
    python model_adapter.py --backend openai_compat --issue microservices_vs_monolith --text "..."

    # 用批量标签后端（读队友产出的 JSONL）
    python model_adapter.py --backend batch_jsonl --labels labels.jsonl --issue ... --text ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, asdict, field
from typing import Any, Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from stance_extractor import ISSUE_TAXONOMY, resolve_side  # noqa: E402

ISSUE_BY_ID = {i["issue_id"]: i for i in ISSUE_TAXONOMY}
ISSUE_BY_SHORT = {i["issue_id"].split("_")[0]: i for i in ISSUE_TAXONOMY}


# ---------------------------------------------------------------------------
# 1. 统一输出结构
# ---------------------------------------------------------------------------
@dataclass
class Judgment:
    """一次判立场的结果。所有后端都必须吐这个。"""
    issue_id: str = ""
    supports: str = ""          # 用本系统的 side 名（如 "微服务"）
    side: str = ""              # a / b / neutral / undetermined / unknown
    confidence: float = 0.0
    evidence_span: str = ""     # ⚠️ 分类模型通常给不出这个字段
    reasoning: str = ""
    backend: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """能不能进反仓库：必须明确站了一方，且有对立面。"""
        return self.side in ("a", "b") and bool(self.issue_id)

    def digest(self) -> dict:
        d = asdict(self)
        d["usable"] = self.usable
        return d


def text_key(text: str) -> str:
    """候选文本的稳定键，用于和队友产出的标签文件对齐。"""
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 2. 后端：OpenAI 兼容接口
# ---------------------------------------------------------------------------
class OpenAICompatBackend:
    """走 OpenAI 兼容 HTTP 接口。现在用 GLM 免费档；换自训模型只要改 .env 三个变量。

    实现上**不重写调用逻辑**，而是直接转调 `stance_extractor.extract()` ——
    这样 prompt、JSON 容错、证据闸门、重试、缓存全部复用同一套，判据不会分叉。
    （教训：两处各写一份 prompt 迟早会不一致。）
    """

    name = "openai_compat"

    def __init__(self, *_args, **_kwargs):
        # 配置全部来自 code/.env（OPENAI_API_KEY / OPENAI_BASE_URL / STANCE_MODEL），
        # 由 stance_extractor 负责加载。这里不接受参数，避免出现两套配置源。
        pass

    def judge(self, texts: list[str], issue_id: str = "") -> list[Judgment]:
        import stance_extractor as se

        taxonomy = [ISSUE_BY_ID[issue_id]] if issue_id in ISSUE_BY_ID else None
        out: list[Judgment] = []
        for t in texts:
            try:
                stances = se.extract(t, taxonomy)
            except Exception as e:
                out.append(Judgment(issue_id=issue_id, side="unknown", backend=self.name,
                                    reasoning=f"抽取失败：{e}"))
                continue
            if not stances:
                out.append(Judgment(issue_id=issue_id, side="unknown", backend=self.name,
                                    reasoning="未抽到明确立场（可能被判 neutral/undetermined）"))
                continue
            for s in stances:
                out.append(Judgment(
                    issue_id=getattr(s, "issue_id", issue_id) or issue_id,
                    supports=getattr(s, "supports", "") or "",
                    side=getattr(s, "side", "unknown"),
                    confidence=float(getattr(s, "confidence", 0) or 0),
                    evidence_span=getattr(s, "evidence_span", "") or "",
                    reasoning=getattr(s, "reasoning", "") or "",
                    backend=self.name,
                    raw={"strength": getattr(s, "strength", "")},
                ))
        return out


# ---------------------------------------------------------------------------
# 3. 后端：批量标签文件（★ 队友训的模型推荐走这条）
# ---------------------------------------------------------------------------
class BatchJsonlBackend:
    """读预先算好的标签。零加载延迟、现场不会崩、队友不在场也能演示。

    标签文件的格式（每行一条 JSON）—— 这就是**要给队友的交付格式**：

        {"key": "<候选文本的 sha1 前16位>", "label": "微服务", "prob": 0.93}
        {"text": "<原始文本>", "label": "单体", "prob": 0.88}

    两种定位方式任选：`key`（推荐，文本很长时更省空间）或 `text`（直白，便于人工核对）。

    标签怎么映射到本系统的议题与立场，由 `label_map` 决定 —— 见 label_map.example.json。
    """

    name = "batch_jsonl"

    def __init__(self, labels_path: str, label_map: dict[str, dict]):
        self.labels_path = labels_path
        self.label_map = label_map
        self.by_key: dict[str, dict] = {}
        self.by_text: dict[str, dict] = {}
        if labels_path and os.path.exists(labels_path):
            with open(labels_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("key"):
                        self.by_key[str(rec["key"])] = rec
                    if rec.get("text"):
                        self.by_text[str(rec["text"]).strip()] = rec

    def judge(self, texts: list[str], issue_id: str = "") -> list[Judgment]:
        out: list[Judgment] = []
        for t in texts:
            rec = self.by_key.get(text_key(t)) or self.by_text.get(t.strip())
            if not rec:
                out.append(Judgment(issue_id=issue_id, side="unknown", backend=self.name,
                                    reasoning="标签文件里没有这条文本"))
                continue
            label = str(rec.get("label"))
            mapped = self.label_map.get(label) or {}
            mapped_issue = mapped.get("issue_id") or issue_id
            supports = mapped.get("supports") or ""
            issue = ISSUE_BY_ID.get(mapped_issue)
            side = resolve_side(issue, supports) if issue else "unknown"
            out.append(Judgment(
                issue_id=mapped_issue,
                supports=supports,
                side=side,
                confidence=float(rec.get("prob") or 0.0),
                # ⚠️ 分类模型给不出证据 —— 这里只能是空，下游要靠生成式模型补
                evidence_span="",
                reasoning=f"队友模型标签：{label}",
                backend=self.name,
                raw=rec,
            ))
        return out


# ---------------------------------------------------------------------------
# 4. 后端：本地模型文件（需要队友提供一个调用样例）
# ---------------------------------------------------------------------------
class LocalFileBackend:
    """直接加载本地模型文件。

    ⚠️ 这个后端**需要队友给一段三行的调用样例**才能真正跑起来，
    因为模型文件的形式太多了，加载方式完全不同：

        · HuggingFace 目录（config.json + model.safetensors + tokenizer）
              → transformers 的 AutoModelForSequenceClassification + AutoTokenizer
        · PyTorch .pt / .pth      → torch.load，需要模型定义代码
        · sklearn .pkl / .joblib  → joblib.load，通常要 TF-IDF 向量化器一起存
        · ONNX .onnx              → onnxruntime，需要 tokenizer 配套
        · GGUF（llama.cpp 量化）   → llama-cpp-python，CPU 也能跑
        · 也可能是 sentence-transformers 的 embedding（那不能做分类，见下）

    所以这里**没有预置加载逻辑**，改成接收一个调用函数 `infer_fn(texts) -> list[dict]`。
    队友只要写：

        def infer(texts: list[str]) -> list[dict]:
            # 返回 [{"label": "...", "prob": 0.9}, ...]
            ...

    然后：
        python model_adapter.py --backend local_file --module 他的模块路径
    """

    name = "local_file"

    def __init__(self, module_path: str = "", func_name: str = "infer",
                 label_map: dict[str, dict] | None = None):
        self.label_map = label_map or {}
        self.infer_fn: Callable[[list[str]], list[dict]] | None = None
        if module_path:
            import importlib.util

            spec = importlib.util.spec_from_file_location("teammate_model", module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"无法加载模块：{module_path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, func_name, None)
            if fn is None:
                raise RuntimeError(f"{module_path} 里没有函数 {func_name}()")
            self.infer_fn = fn

    def judge(self, texts: list[str], issue_id: str = "") -> list[Judgment]:
        if self.infer_fn is None:
            raise RuntimeError("没有提供 --module，无法推理（LocalFileBackend 需要调用样例）")
        results = self.infer_fn(texts)
        out: list[Judgment] = []
        for rec in results:
            label = str(rec.get("label"))
            mapped = self.label_map.get(label) or {}
            mapped_issue = mapped.get("issue_id") or issue_id
            supports = mapped.get("supports") or ""
            issue = ISSUE_BY_ID.get(mapped_issue)
            out.append(Judgment(
                issue_id=mapped_issue,
                supports=supports,
                side=resolve_side(issue, supports) if issue else "unknown",
                confidence=float(rec.get("prob") or 0.0),
                evidence_span="",
                reasoning=f"本地模型标签：{label}",
                backend=self.name,
                raw=rec,
            ))
        return out


BACKENDS = {
    "openai_compat": OpenAICompatBackend,
    "batch_jsonl": BatchJsonlBackend,
    "local_file": LocalFileBackend,
}


class MockBackend(OpenAICompatBackend):
    """离线假模型（规则桩）。

    用途：没有 API key、或者现场断网时，**仍能把整条流水线跑通**，
    供演示兜底与自检使用。它只是规则桩，**准确率没有参考价值**，不要用它跑评测。
    """

    name = "mock"

    def judge(self, texts: list[str], issue_id: str = "") -> list[Judgment]:
        import stance_extractor as se

        old = se._USE_MOCK
        se._USE_MOCK = True
        try:
            res = super().judge(texts, issue_id)
        finally:
            se._USE_MOCK = old
        for r in res:
            r.backend = self.name
        return res


BACKENDS["mock"] = MockBackend


# ---------------------------------------------------------------------------
# 5. 标签映射 + 自检
# ---------------------------------------------------------------------------
def load_label_map(path: str) -> dict[str, dict]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("labels") or {}


def selftest() -> int:
    print("=" * 72)
    print("模型适配层 —— 离线自检")
    print("=" * 72)
    problems: list[str] = []

    # 1) 标签映射能不能把队友的标签翻成本系统的立场
    label_map = {"微服务": {"issue_id": "microservices_vs_monolith", "supports": "微服务"},
                 "单体": {"issue_id": "microservices_vs_monolith", "supports": "单体"},
                 "兴趣_架构": {"issue_id": "", "supports": ""}}
    t1 = "我们上了微服务之后，排障时间翻了三倍"
    t2 = "拆分之后各小组可以独立发布，效率高多了"
    os.makedirs(_tmpdir(), exist_ok=True)
    labels_path = os.path.join(_tmpdir(), "_selftest_labels.jsonl")
    with open(labels_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"key": text_key(t1), "label": "单体", "prob": 0.91}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"key": text_key(t2), "label": "微服务", "prob": 0.87}, ensure_ascii=False) + "\n")

    be = BatchJsonlBackend(labels_path, label_map)
    res = be.judge([t1, t2])

    checks = [
        ("批量后端读到了标签", all(r.backend == "batch_jsonl" for r in res), ""),
        ("标签 单体 → 支持方=单体", res[0].supports == "单体", f"实际 {res[0].supports}"),
        ("标签 单体 → side=b", res[0].side == "b", f"实际 {res[0].side}"),
        ("标签 微服务 → side=a", res[1].side == "a", f"实际 {res[1].side}"),
        ("两条都可用（能进反库）", all(r.usable for r in res), ""),
        ("★ 分类后端给不出证据（已知局限）", res[0].evidence_span == "",
         "这是设计上的错配，下游要靠生成式模型补证据"),
    ]

    # 2) 兴趣类标签不能当立场用
    be2 = BatchJsonlBackend(labels_path, {"兴趣_架构": {"issue_id": "", "supports": ""}})
    r2 = be2.judge([t1])[0]
    checks.append(("兴趣类标签 → 判不出立场，会被丢掉",
                   not r2.usable and r2.side == "unknown", f"side={r2.side}"))

    # 3) 缺失标签要能优雅失败
    r3 = be.judge(["一条标签文件里没有的文本"])[0]
    checks.append(("标签缺失 → unknown，不抛异常", r3.side == "unknown", ""))

    for name, ok, note in checks:
        print(f"{'✅' if ok else '❌'} {name}" + (f"   （{note}）" if note else ""))
        if not ok:
            problems.append(name)

    try:
        os.remove(labels_path)
    except OSError:
        pass

    print("-" * 72)
    if problems:
        print(f"❌ 未通过：{problems}")
        return 1
    print("✅ 全部通过")
    print()
    print("提醒：LocalFileBackend 没有预置加载逻辑 —— 模型文件形式太多（HF 目录 / .pt /")
    print("      .pkl / .onnx / .gguf），加载方式完全不同。请队友给一段三行的调用样例。")
    return 0


def _tmpdir() -> str:
    import tempfile

    return tempfile.gettempdir()


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="模型适配层：判立场用哪个后端")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--backend", default="batch_jsonl", choices=list(BACKENDS))
    ap.add_argument("--text", nargs="*", default=[], help="要判的一条或多条文本")
    ap.add_argument("--issue", default="", help="议题 id（标签映射里没写 issue 时用）")
    ap.add_argument("--labels", default="", help="batch_jsonl 的标签文件路径")
    ap.add_argument("--module", default="", help="local_file 的 Python 模块路径")
    ap.add_argument("--label-map", default=os.path.join(_HERE, "label_map.example.json"))
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.text:
        ap.print_help()
        return 0

    lmap = load_label_map(args.label_map)
    if args.backend == "batch_jsonl":
        be = BatchJsonlBackend(args.labels, lmap)
    elif args.backend == "local_file":
        be = LocalFileBackend(args.module, label_map=lmap)
    else:
        be = OpenAICompatBackend()

    for j in be.judge(args.text, args.issue):
        print(json.dumps(j.digest(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
