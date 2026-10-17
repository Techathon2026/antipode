#!/usr/bin/env python3
"""省流 —— 把一份长材料压成「给用户看的两三句话」。

为什么单独一个模块
------------------
GitHub 的 README 和知乎的长回答，要的是**同一种输出**：这段东西在讲什么、
值不值得点进去。所以摘要不写在采集器里，抽出来共用一份 prompt，
省得两边各写一版、越改越不像。

和「立场抽取」的分工
--------------------
立场抽取（`stance_extractor`）回答"作者站哪边"，用于反推荐；
省流（本模块）回答"这篇在说什么"，用于信息收集。
两件事不能混：省流的 prompt 里**不允许出现任何立场判断**，
否则摘要会替用户选边，反推荐那一步就没得做了。

输出结构
--------
    {"summary": "两到三句中文", "points": ["要点1", "要点2", "要点3"]}

`points` 允许为空（有些材料只有一句话可说）。任何字段缺了都不算失败，
调用方按缺省处理 —— 摘要坏掉不该阻断入库。
"""

from __future__ import annotations

import json
import os
import re

# 导入 stance_extractor 只为触发它加载 code/.env（配置只有一处，不另开一套）。
try:                                                           # noqa: F401
    import stance_extractor as _se
except Exception:                                              # noqa: BLE001
    _se = None


# ---------------------------------------------------------------------------
# 清洗：README 里一半是徽章、图片、HTML 注释，喂给模型纯属浪费
# ---------------------------------------------------------------------------
_RE_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")                  # ![alt](url)
_RE_HTML = re.compile(r"<[^>]{1,200}>")                        # <img ...> <br>
_RE_LINK = re.compile(r"\[([^\]]{0,120})\]\([^)]{0,300}\)")    # [text](url)
_RE_BADGE_LINE = re.compile(r"^\[!\[|^!\[|^<p align|^<h\d align", re.I)
_RE_HR = re.compile(r"^\s*([-*_]\s*){3,}$")
_RE_CODEBLOCK = re.compile(r"```.*?```", re.S)


def clean_markdown(text: str, keep_code: bool = False) -> str:
    """把 README / 富文本洗成「能读的纯文字」。

    保留的：标题、正文、列表、表格文字。
    丢掉的：图片、徽章、HTML 标签、裸链接地址（留下文字）。
    """
    t = text or ""
    if not keep_code:
        # 代码块只留第一行注释之外的提示语，不留整段源码：
        # 摘要要的是"这东西干什么"，不是实现细节。
        t = _RE_CODEBLOCK.sub(" ", t)
    t = _RE_HTML.sub("", t)
    t = _RE_IMG.sub("", t)
    t = _RE_LINK.sub(r"\1", t)

    out: list[str] = []
    for line in t.splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        if _RE_BADGE_LINE.match(s):                            # 徽章行整行丢
            continue
        if _RE_HR.match(s):
            continue
        out.append(s)

    t = "\n".join(out)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def clip(text: str, max_chars: int = 7000) -> str:
    """长材料取前 N 字。

    为什么取开头而不随机抽：README 和回答都是**把结论放前面**的结构，
    开头一段的信息密度最高；后面的安装步骤、许可证、致谢全是噪声。
    """
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    return t[:max_chars] + "\n…（后文省略）"


# ---------------------------------------------------------------------------
# 调模型
# ---------------------------------------------------------------------------
_SUMMARY_PROMPT = """你在帮人做信息筛选。读下面的材料，用中文写一段省流。

要求：
1. summary：两到三句话，说清「这是什么 / 解决什么问题 / 有什么特点」。
   不超过 110 个汉字。不要出现「本文」「该文档」「作者」这类词，
   直接说事。
2. points：最多 3 条，每条不超过 20 个汉字，是这个材料里**最值得知道的具体信息**
   （具体数字、具体能力、具体限制）。没有具体信息就给空数组。
3. 只做客观转述。**不要评价好坏，不要判断观点对错，不要推测作者立场。**
4. 材料是英文就仍然用中文写省流。

只输出 JSON，不要解释：
{"summary": "...", "points": ["...", "..."]}
"""


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def available() -> bool:
    """有没有可用的模型配置。没有就退化成"取首段当省流"。"""
    return bool(_env("OPENAI_API_KEY"))


def summarize(title: str, text: str, extra: str = "",
              max_chars: int = 7000, timeout: float = 60.0) -> dict:
    """把一段材料压成 {summary, points}。

    模型不可用时不报错 —— 退化成 extractive 兜底（取开头第一段），
    保证采集流程永远能走完。省流是锦上添花，不该成为入库的单点故障。
    """
    body = clip(clean_markdown(text), max_chars)
    if not body:
        return {"summary": (title or "").strip()[:110], "points": [],
                "summary_from": "title"}

    if not available():
        return _fallback(title, body, "no_key")

    user = f"标题：{title}\n" + (f"附加信息：{extra}\n" if extra else "") \
        + f"\n材料：\n{body}"

    try:
        raw = _chat([
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": user},
        ], timeout=timeout)
    except Exception as e:                                     # noqa: BLE001
        return _fallback(title, body, f"llm_error:{type(e).__name__}")

    got = _parse_json(raw)
    if not got:
        return _fallback(title, body, "bad_json")

    summary = str(got.get("summary") or "").strip()
    points = [str(x).strip() for x in (got.get("points") or [])
              if str(x).strip()][:3]
    if not summary:
        return _fallback(title, body, "empty_summary")
    return {"summary": summary[:220], "points": points, "summary_from": "llm"}


def _fallback(title: str, body: str, why: str) -> dict:
    """模型不可用时的退化路径：取正文第一段，去掉 Markdown 记号。"""
    first = ""
    for para in body.split("\n\n"):
        s = para.strip().lstrip("#>-* ").strip()
        if len(s) >= 20:
            first = s
            break
    first = re.sub(r"\s+", " ", first)
    line = (first or title or "").strip()[:150]
    return {"summary": line, "points": [], "summary_from": why}


def _chat(messages: list[dict], timeout: float = 60.0) -> str:
    """一次 OpenAI 兼容调用。

    只在省流这一处需要"自由生成"，所以不绕 `stance_extractor`（那边是
    立场抽取的专用链路，prompt 与容错都不一样）。配置仍共用 code/.env。
    """
    from openai import OpenAI

    key = _env("OPENAI_API_KEY")
    base = _env("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = _env("STANCE_MODEL") or _env("SUMMARY_MODEL") or "glm-4-flash-250414"

    client = OpenAI(api_key=key, base_url=base, timeout=timeout)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=600,
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_json(raw: str) -> dict:
    """模型很爱在 JSON 外面包 ```json 围栏，或者多写一句话。"""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I | re.M).strip()
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:                                          # noqa: BLE001
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return {}
        try:
            d = json.loads(m.group(0))
            return d if isinstance(d, dict) else {}
        except Exception:                                      # noqa: BLE001
            return {}


if __name__ == "__main__":                                    # 手测
    demo = ("# Handwired\n\nTurn your regular JavaScript code into a neural net; "
            "no training, exact, yet also fully differentiable!\n\n"
            "## How It Works\n\nA program's made of if-else, loops, and state.")
    print(json.dumps(summarize("chenglou/handwired", demo),
                     ensure_ascii=False, indent=2))
