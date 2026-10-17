#!/usr/bin/env python3
"""知乎信息流的垃圾过滤：广告、盐选小说、以及"看着像文章其实没内容"的东西。

为什么必须自己做
------------
知乎的信息流是**混装**的：真观点、品牌广告、盐选小说、以及一段引战标题配
三行字的空壳，全在同一个列表里。不筛，反推荐就会去给一条广告找"对立面"，
既荒谬又浪费模型调用。所以这一步在**建库之前**，属于准入门槛。

判据为什么分三层
------------
只靠关键词会误杀：
  - 「如何评价某部小说」是正经书评，不能因为是"小说"就丢；
  - 「重生」出现在商业复盘里也说得通（"重生后的品牌"）。
所以三层递进，**每层给出可解释的原因**，宁可漏掉一条广告也不误杀一篇好文章：

  1. **结构层**（最可信）：DOM 上有广告位标记、URL 落在广告跳转域或 /market/
  2. **标记层**：标题/摘要里带「广告」「盐选」「会员专享」这类平台自己打的标
  3. **文风层**（最弱，需多个特征同时命中）：章节号 + 第一人称 + 网文套路词

任何一条命中都会记下 `reason`，日报和答辩时能直接说清"这条为什么被丢"。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 第 1 层：结构性判据（几乎不会误判）
# ---------------------------------------------------------------------------
AD_HOSTS = (
    "link.zhihu.com",          # 站外跳转，广告和商品卡都走这里
    "click.zhihu.com",
    "e.zhihu.com",
)
AD_URL_MARKS = ("utm_source=ad", "utm_medium=ad", "ad_id=")
# 注意：/market/ 是**付费内容**不是广告，不能放进 AD_URL_MARKS ——
# 两者的处理结果都是丢弃，但原因说错了，答辩时会被问住。

# 盐选付费内容 / 会员专享：这两个路径下的东西是"卖货"，不是"表达"
PAYWALL_URL_MARKS = ("/market/", "/pay/", "zhuanlan.zhihu.com/pay")

# ---------------------------------------------------------------------------
# 第 2 层：平台自己打的标
# ---------------------------------------------------------------------------
AD_LABELS = ("广告", "赞助", "推广", "品牌合作", "商业推广")

PAYWALL_LABELS = (
    "盐选", "盐选专栏", "盐选推荐", "会员专享", "付费内容",
    "开通会员", "继续阅读需付费",
)

# ---------------------------------------------------------------------------
# 第 3 层：文风判据（必须多条同时命中）
# ---------------------------------------------------------------------------
_CHAPTER = re.compile(r"第\s*[一二三四五六七八九十百零\d]+\s*[章节回]")
_FIRST_PERSON = re.compile(r"^(我|我们|本人)(被|在|重生|穿越|觉醒|回到|成了)")
_WEB_NOVEL = (
    "重生", "穿越", "快穿", "系统绑定", "追妻", "虐恋", "霸总", "校花",
    "赘婿", "龙王", "修仙", "飞升", "渡劫", "末世", "灵气复苏",
)
# 「已完结 / 全文 / 大结局」这类标记，正常文章基本不会写在标题里
_SERIAL_MARK = ("（完）", "(完)", "全文", "大结局", "已完结", "完结文")

# ---------------------------------------------------------------------------
# 第 4 层：种草 / 导购软文
# ---------------------------------------------------------------------------
# 知乎的软文比硬广更难认：写得像测评，其实是带货。共同点是**标题里堆商品
# 参数和促销词**，而真正的观点文章不会这样写标题。实测样本：
#   「100寸超高刷电视推荐：真彩RGB-Mini LED电视爆款王…4K 180Hz 原生高刷」
#   「数字IC设计培训推荐（AI芯片22nm流片项目实操）」
# 判据仍是组合拳：只出现「推荐」不算（《为什么我不推荐初学者直接学 Rust》
# 是正经观点），必须再叠上参数或第二个导购词才判 —— 与网文判据同一条原则。
_SHOPPING_WORDS = (
    "种草", "测评", "横评", "实测", "爆款", "限时", "优惠", "领券", "下单",
    "好物", "团购", "选购", "哪款", "怎么选", "平替", "折扣", "秒杀", "直降",
    "到手价", "闭眼入", "回购", "拔草", "攻略", "推荐",
)
_SPEC_TOKEN = re.compile(
    r"\d+\s*(?:nm|μm|um|mm|cm|寸|英寸|Hz|GB|TB|mAh|毫安|万|元|W\b)")
_MODEL_TOKEN = re.compile(
    r"(?:[A-Za-z]{1,6}\d{2,4}[A-Za-z]{0,3}|[A-Z]{2,}[-\s]?\d{2,4})")


def classify(*, url: str = "", title: str = "", text: str = "",
             dom_is_ad: bool = False, dom_labels: tuple[str, ...] = ()
             ) -> dict:
    """判断一条信息该不该进库。

    返回 `{"drop": bool, "junk": "ad"|"fiction"|"empty"|"", "reason": str}`。
    `drop=False` 表示通过，`junk` 为空。
    """
    u = (url or "").strip().lower()
    t = (title or "").strip()
    body = (text or "").strip()
    blob = f"{t}\n{body[:600]}"
    labels = tuple(dom_labels or ())

    # ---- 1. 结构层 -------------------------------------------------------
    if dom_is_ad:
        return _drop("ad", "信息流广告位（DOM 上带广告标记）")

    if labels:
        if any(x in " ".join(labels) for x in AD_LABELS):
            return _drop("ad", f"平台标注为广告：{'/'.join(labels)}")
        if any(x in " ".join(labels) for x in PAYWALL_LABELS):
            return _drop("fiction", f"平台标注为付费内容：{'/'.join(labels)}")

    for h in AD_HOSTS:
        if h in u:
            return _drop("ad", f"跳转到站外广告域：{h}")
    for m in AD_URL_MARKS:
        if m in u:
            return _drop("ad", f"URL 带广告/商品特征：{m}")
    for m in PAYWALL_URL_MARKS:
        if m in u:
            return _drop("fiction", f"URL 落在付费墙内：{m}")

    # ---- 2. 标记层 -------------------------------------------------------
    head = f"{t} {body[:80]}"
    if any(x in head for x in AD_LABELS):
        return _drop("ad", "标题/摘要自带「广告」标记")
    if any(x in head for x in PAYWALL_LABELS):
        return _drop("fiction", "标题/摘要自带「盐选/会员」标记")

    # ---- 3. 文风层：数命中数，够多才算 --------------------------------
    hits: list[str] = []
    if _CHAPTER.search(t):
        hits.append("章节号")
    if _FIRST_PERSON.search(t):
        hits.append("第一人称故事开头")
    novel = [w for w in _WEB_NOVEL if w in t]
    if novel:
        hits.append("网文套路词:" + "/".join(novel[:3]))
    if any(m in t for m in _SERIAL_MARK):
        hits.append("完结标记")

    # 单条「重生」不足以定性，但「章节号 + 完结标记」两条就非常明确了。
    # 阈值取 2：网文的标题几乎总是叠着两三个特征一起用。
    if len(hits) >= 2:
        return _drop("fiction", "标题像网文：" + "、".join(hits))

    # ---- 4. 种草 / 导购软文 ---------------------------------------------
    # 组合条件：必须有导购词，且再叠上「第二个导购词 / 商品参数 / 型号」之一。
    shop = [w for w in _SHOPPING_WORDS if w in t]
    if shop:
        spec = _SPEC_TOKEN.findall(t)
        model = _MODEL_TOKEN.findall(t)
        if len(shop) >= 2 or spec or model:
            detail = "、".join(shop[:3])
            if spec:
                detail += f" +参数「{spec[0].strip()}」"
            elif model:
                detail += f" +型号「{model[0]}」"
            return _drop("ad", "标题像种草/导购：" + detail)

    # ---- 5. 空壳：有标题没内容 ------------------------------------------
    # 信息流里大量「三行字 + 引战标题」的帖子，判立场时会被当成"无观点"，
    # 与其让引擎白跑一趟，不如在准入就把它们标出来。
    if not body and not t:
        return _drop("empty", "标题和正文都为空")
    if len(body) < 30 and not u:
        return _drop("empty", "只有一句话，没有可读内容")

    return {"drop": False, "junk": "", "reason": ""}


def _drop(junk: str, reason: str) -> dict:
    return {"drop": True, "junk": junk, "reason": reason}


def explain(d: dict) -> str:
    """给日志/演示用的一句话。"""
    return f"{d['junk']}：{d['reason']}" if d.get("drop") else "通过"


if __name__ == "__main__":                                    # 手测
    cases = [
        dict(url="https://www.zhihu.com/question/123/answer/456",
             title="为什么年轻人不再愿意加班？",
             text="我觉得根本原因在于投入产出比的变化，过去加班能换来晋升，" * 3),
        dict(url="https://link.zhihu.com/?target=xxx", title="某品牌新款耳机"),
        dict(url="https://www.zhihu.com/market/paid_column/123",
             title="第三章 他在雨里等了我一夜"),
        dict(url="https://www.zhihu.com/question/1/answer/2",
             title="重生后我嫁给了渣男的死对头（完）",
             text="我重生了，回到十八岁那年。"),
        dict(url="https://www.zhihu.com/question/1/answer/3",
             title="广告 | 学英语就选这家",
             text="点击了解详情"),
        # --- 第 4 层：用今天真实采到的两条软文验证，另配两条不该误杀的 ---
        dict(url="https://zhuanlan.zhihu.com/p/2083880604035302095",
             title="100寸超高刷电视推荐：真彩RGB-Mini LED电视爆款王海信小墨E5S Pro，"
                   "4K 180Hz原生高刷太丝滑了",
             text="最近入手了这台电视"),
        dict(url="https://zhuanlan.zhihu.com/p/2057479775011263946",
             title="数字IC设计培训推荐（AI芯片22nm流片项目实操）",
             text="课程包含流片"),
        # 只出现「推荐」但不是带货 —— 必须保住
        dict(url="https://zhuanlan.zhihu.com/p/1",
             title="为什么我不推荐初学者直接学 Rust",
             text="因为所有权模型需要前置知识，" * 5),
        # 有参数但没有导购词 —— 技术讨论，必须保住
        dict(url="https://zhuanlan.zhihu.com/p/2",
             title="为什么 H100 的 80GB 显存仍然不够用",
             text="大模型的 KV cache 会随上下文线性增长，" * 5),
    ]
    for c in cases:
        d = classify(**c)
        print(f"{'丢弃' if d['drop'] else '保留'}  {c['title'][:28]:<30} {explain(d)}")
