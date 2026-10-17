"""议题 → 检索词表

【这张表是 2026-10-16 实测出来的，不是拍脑袋写的】

反引擎要去找"与库条目相反的东西"，但**不能搜索反义词**（搜"微服务 不好"什么都搜不到）。
正确做法：搜**对立面自己的名字**。

═══ 实测记录 A：英文源（HN）═══
    ✅ 搜 `monolith`（HN 文章标题）→ 一排名篇，全是反对微服务的：
       "Goodbye Microservices"、"Monoliths Are the Future"、"Monolith First"
       —— 对立面的名字本身就是最好的检索词
    ✅ 搜 `"premature microservices"`（精确短语）→ 精确度 100%，但只有 1 条（召回≈0）
    ❌ 搜 `"we moved back to a monolith"` → 0 条
    ❌ HN **评论区**搜索不能用 → Algolia 做前缀匹配，"monolithic"/"monolithically"
       全被算命中，捞回 2015 年的招聘帖

═══ 实测记录 B：中文源（V2EX / 掘金）═══
    ✅ 搜 `单体架构`（V2EX）→ 「微服务的缺点」(81 回复)、
       「仔细想想，微服务架构相比于单体服务架构来讲优势也没有很大」(56 回复)、
       「2024 年了，有多少公司和系统由微服务/云原生转为了单体架构?」
       —— 中文第一手经验与吐槽，质量很好
    ✅ 搜 `过度设计`（V2EX）→ 「Java 开发，你会过度设计吗」、
       「今天被组长好心提醒了下，不要过度设计」(44 回复)
    ❌ 搜 `微服务 后悔`（V2EX）→ 捞回"后悔做产品经理""结婚之后是男方后悔多""抽烟会不会后悔"
       —— **空格被当成松散 OR**，多词查询严重退化
    ⚠️ 掘金偏教程（"微服务设计原则""六种常用设计模式"），观点密度低于 V2EX，只作补充
    ⚠️ 掘金搜 `单体架构` 返回 **0 条**，召回不如 V2EX 稳定

→ 结论：**检索词一律压到 1–2 个词，最好就是对立面的名字本身。**
   召回宁可多带噪声，筛掉噪声是 stance_extractor 的活。

═══ ★ 中文特有的坑：很多方**没有"立场名"** ═══
英文里 `monolith` 是一个真立场名 —— 拥护单体的人会自称 monolith 派，所以搜它对得上。
中文不成立：搜 `坐班` 捞回来的是**招聘帖**（"招聘：需驻场坐班开发""求活，可短时坐班"），
因为"坐班"只是中性的工作形态描述词，支持坐班的人不会自称"坐班派"。

→ 所以中文的正确打法是：**议题词召回，模型筛立场**。
   下面标了 `无立场名` 注释的那些方，检索词直接放议题词，靠模型把它筛出来。

═══ 关于语言 ═══
仓库内容是**中文为主**，所以检索词以中文为主、英文为辅，检索源默认也是中文优先
（否则中文库旁边配一堆英文文章，演示观感割裂）。
有些议题的"反面"在中文里没有独立说法（比如 TDD 的反面），这类就用议题词召回，
靠模型判定立场 —— **能召回就行，判定不靠词表。**

字段结构：
    ISSUE_TERMS[issue_id] = {
        "topics": [...],            # 议题本身的关键词，宽召回用
        "sides": {                  # 每一方的检索词，精确召回用
            "<side_a>": [...],
            "<side_b>": [...],
        },
        "retrieval": "auto" | "none",   # none = 该议题不做检索（人工预置演示用）
    }

约束：
    · issue_id 必须来自 stance_extractor.ISSUE_TAXONOMY，不许自创
    · sides 的 key 必须与 side_a / side_b 逐字一致（这是外键）
    · 检索词最多 2 个词（超过会被自检拦下）
"""

import re

TERMS_VERSION = "v2-中英双语"

# 这两个议题不做检索：一个是纯梗（Tab vs 空格），一个是生活偏好（豆腐脑）。
# 它们的反信息由人工预置，用来演示 counter_type = 视野缺失(C) 那一类。
NO_RETRIEVAL = ("tabs_vs_spaces", "tofu_pudding")

ISSUE_TERMS: dict[str, dict] = {
    "microservices_vs_monolith": {
        "topics": ["微服务", "单体", "microservices", "monolith"],
        "sides": {
            "微服务": ["微服务", "microservices"],
            "单体": ["单体", "monolith"],
        },
        "retrieval": "auto",
    },
    "tdd_necessity": {
        "topics": ["TDD", "单元测试", "测试驱动"],
        "sides": {
            "先写测试": ["TDD", "测试驱动"],
            # 无立场名：中文没有"反 TDD 派"的自称，用议题词召回再靠模型筛
            "先写实现": ["TDD", "不写测试"],
        },
        "retrieval": "auto",
    },
    "monorepo_vs_multirepo": {
        "topics": ["monorepo", "单仓", "多仓"],
        "sides": {
            "单体仓库": ["monorepo", "单仓"],
            "多仓库": ["multirepo", "多仓"],
        },
        "retrieval": "auto",
    },
    "react_vs_vue": {
        "topics": ["React", "Vue", "前端框架"],
        "sides": {
            "React": ["React", "reactjs"],
            "Vue": ["Vue", "vuejs"],
        },
        "retrieval": "auto",
    },
    "rest_vs_graphql": {
        "topics": ["GraphQL", "REST", "接口设计"],
        "sides": {
            "REST": ["REST", "RESTful"],
            "GraphQL": ["GraphQL"],
        },
        "retrieval": "auto",
    },
    "orm_or_raw_sql": {
        "topics": ["ORM", "SQL", "数据访问"],
        "sides": {
            "ORM": ["ORM"],
            "手写 SQL": ["手写SQL", "raw-SQL"],
        },
        "retrieval": "auto",
    },
    "typescript_strictness": {
        "topics": ["TypeScript", "严格模式", "类型"],
        "sides": {
            "开严格模式": ["严格模式", "strict"],
            # 无立场名：没人自称"反严格模式派"，用议题词召回
            "关严格模式": ["TypeScript", "类型体操"],
        },
        "retrieval": "auto",
    },
    "oop_vs_fp": {
        "topics": ["面向对象", "函数式", "OOP"],
        "sides": {
            "面向对象": ["面向对象", "OOP"],
            "函数式": ["函数式", "FP"],
        },
        "retrieval": "auto",
    },
    "ai_assistant_weakens_dev": {
        "topics": ["AI 编程", "Copilot", "代码生成"],
        "sides": {
            "AI 助手有害": ["过度依赖", "AI 依赖"],
            "AI 助手有益": ["AI 提效", "Copilot"],
        },
        "retrieval": "auto",
    },
    "remote_vs_onsite": {
        "topics": ["远程办公", "坐班", "返岗"],
        "sides": {
            "远程办公": ["远程办公", "remote work"],
            # 无立场名：搜"坐班"会捞回一堆招聘帖（已实测），所以用议题词召回
            "坐班": ["远程办公", "返岗"],
        },
        "retrieval": "auto",
    },
    "rust_vs_cpp": {
        "topics": ["Rust", "C++", "内存安全"],
        "sides": {
            "Rust": ["Rust", "rustlang"],
            "C++": ["C++", "cpp"],
        },
        "retrieval": "auto",
    },
    "cloud_vs_selfhost": {
        "topics": ["上云", "自建", "私有化"],
        "sides": {
            "上云": ["上云", "cloud migration"],
            "自建": ["自建", "私有化部署"],
        },
        "retrieval": "auto",
    },
    "code_review_strictness": {
        "topics": ["代码评审", "code review"],
        "sides": {
            "严格评审": ["代码评审", "code review"],
            # 无立场名：用议题词召回
            "宽松评审": ["代码评审", "评审太慢"],
        },
        "retrieval": "auto",
    },
    "static_vs_dynamic_typing": {
        "topics": ["静态类型", "动态类型", "类型系统"],
        "sides": {
            "静态类型": ["静态类型", "type safety"],
            "动态类型": ["动态类型", "duck typing"],
        },
        "retrieval": "auto",
    },
    "tabs_vs_spaces": {
        "topics": ["缩进", "tabs", "spaces"],
        "sides": {
            "Tab": ["tab 缩进"],
            "空格": ["空格 缩进"],
        },
        "retrieval": "none",
    },
    "tofu_pudding": {
        "topics": ["豆腐脑"],
        "sides": {
            "甜豆腐脑": ["甜豆腐脑"],
            "咸豆腐脑": ["咸豆腐脑"],
        },
        "retrieval": "none",
    },
}


# ---------------------------------------------------------------------------
# 议题守卫 —— 拦"字面命中但根本不是这场争论"的内容
# ---------------------------------------------------------------------------
#
# 【为什么需要这一层】
# 2026-10-17 整库跑下来的四条脏数据，全是同一个病：**同形异义**。
#
#   议题 ai_assistant_weakens_dev（AI 助手 vs 开发者能力）
#     捞回「翻车了，过度依赖辅助驾驶导致的一次事故记录」
#     ← 命中的是"过度依赖"这个词。辅助驾驶 ≠ AI 编程助手。
#
#   议题 cloud_vs_selfhost（上云 vs 自建）
#     捞回「使用 Nginx 自建 CDN，关于回源问题」
#     ← 命中的是"自建"。这是运维教程，不是"自建还是上云"的取舍。
#
#   议题 static_vs_dynamic_typing（静态类型 vs 动态类型）
#     捞回「Python 里的"鸭子类型"（Duck Typing）」
#     ← 命中的是 side_b 的检索词 duck typing。这是入门教程。
#
# 这三个都不是模型判错，而是**候选本身就不是一场争论的内容**。
# 让模型去判"这条是否反对 X"，它当然能找到理由 —— 问题在候选进错了场子。
#
# 【为什么用规则而不是让模型再答一次】
# 让模型多答一个问题 = 每条候选多一次调用（送检 8 条 → 多 8 次/条目）。
# 而同形异义恰恰是**规则最擅长**的一类：它的定义就是"某个词在这个议题下不代表那个意思"。
# 词表只要跟着实测记录长，规则就够用；模型的判断力留给"立场"这件真正需要判断的事。
#
# 【为什么不改 prompt 加一条"议题对齐"规则】
# SYSTEM_PROMPT 一动，立场抽取的评测基线（41 条 gold_set，90.2%）就作废，必须重跑。
# 在只剩一天多的时候，用一个**确定性、可解释、零成本**的闸门更划算。
# 词表调优本就是这套系统里唯一"像训练"的动作 —— 产出是数据与规则，不是权重。
#
# 结构：
#   TOPIC_GUARD[issue_id] = {
#       "off_topic": r"...",   # 命中它 → 疑似形近异义
#       "unless":    r"...",   # 但正文里出现这些词 → 确实在讲本议题，放行
#       "why":       "...",    # 记录这条是为了拦哪个真实案例（答辩要用）
#   }
#
# 判据：命中 off_topic 且未命中 unless → 判"跑错议题"，不入反库。
# 宁可漏（少一条反信息，用户看到的是空），也不能错（点开是辅助驾驶，产品就废了）。

TOPIC_GUARD: dict[str, dict] = {
    "ai_assistant_weakens_dev": {
        "off_topic": r"辅助驾驶|自动驾驶|智驾|无人驾驶|无人机|AI\s*绘画|AI\s*作图"
                     r"|AIGC|智能音箱|客服机器人|推荐算法|短视频|外卖|导航",
        "unless": r"编程|写代码|代码|开发者|程序员|工程师|Copilot|IDE|重构|调试|bug|仓库",
        "why": "「过度依赖」是通用词，会被辅助驾驶、智能音箱、推荐算法等内容命中",
    },
    "cloud_vs_selfhost": {
        "off_topic": r"自建\s*(CDN|图床|网盘|NAS|博客|DNS|邮件|节点|梯子|机场)"
                     r"|自建\s*CDN|软路由|群晖|折腾\s*NAS",
        "unless": r"云服务器|云主机|上云|云账单|云成本|机房|IDC|服务器成本|迁到云",
        "why": "「自建 CDN / 自建博客」是运维教程，不是「自建还是上云」的取舍",
    },
    "static_vs_dynamic_typing": {
        "off_topic": r"鸭子类型|duck\s*typing|pythonic",
        "unless": r"静态类型|动态类型|类型系统|type\s*safety|type\s*hint|TypeScript|类型检查",
        "why": "「鸭子类型」多数是 Python 入门教程，不是类型系统之争",
    },
    "react_vs_vue": {
        "off_topic": r"源码解析|从零实现|手写\s*(mini|react|vue)|实现原理|编译原理",
        "unless": r"选型|放弃|迁移|转向|换掉|不如|缺点|后悔|替代",
        "why": "框架源码解析/手写实现教程不含立场，《vue3 源码解析》是已知误判点",
    },
    # ---- 下面几条按同一套判据补的：**这个词在本议题里是一个意思，在别处是另一个意思** ----
    "remote_vs_onsite": {
        "off_topic": r"远程(连接|调试|仓库|桌面|控制|协助|登录|访问|调用|推送|注入|部署|执行|服务器)"
                     r"|远程\s*(ssh|SSH|git|Git)|RDP|TeamViewer|向日葵远程|跳板机|VPN 连",
        "unless": r"办公|坐班|通勤|居家|返岗|上班|工位|公司|团队协作|异地|办公室",
        "why": "「远程」在技术语境里默认指远程连接/远程仓库，不是「远程办公还是坐班」",
    },
    "orm_or_raw_sql": {
        "off_topic": r"SQL\s*注入|慢查询|索引优化|分库分表|事务隔离|数据库设计|建表规范|explain\s*分析",
        "unless": r"ORM|Hibernate|MyBatis|JPA|ActiveRecord|Prisma|手写\s*SQL|原生\s*SQL|实体映射",
        "why": "「SQL」相关文章绝大多数是数据库调优，不是「用 ORM 还是手写 SQL」的取舍",
    },
    "code_review_strictness": {
        "off_topic": r"代码规范|ESLint|Prettier|commit\s*(规范|message)|提交规范|Git\s*工作流"
                     r"|pre-commit|husky|lint-staged",
        "unless": r"评审|review|Review|门禁|卡点|审查|把关|合并请求|MR|PR",
        "why": "「规范/检查」类文章讲的是工具配置，不是「评审该严还是该松」",
    },
    "typescript_strictness": {
        "off_topic": r"类型体操|装饰器|泛型(入门|详解|教程)|TypeScript\s*(入门|教程|笔记)"
                     r"|tsconfig\s*配置|类型声明文件",
        "unless": r"strict|严格模式|类型安全|any|ts-ignore|类型检查|类型系统",
        "why": "「TypeScript」教程文遍地都是，大多数不涉及「开不开严格模式」这个取舍",
    },
    "oop_vs_fp": {
        "off_topic": r"23\s*种设计模式|设计模式(详解|大全|入门)|单例模式|工厂模式|观察者模式"
                     r"|UML|面向对象(三大特性|基础)",
        "unless": r"函数式|FP|不可变|纯函数|副作用|柯里化|声明式|组合优于继承",
        "why": "「设计模式」是 OOP 教学，不是「面向对象还是函数式」的路线之争",
    },
    "rust_vs_cpp": {
        "off_topic": r"Rust\s*(安装|环境配置|入门|教程|笔记)|Cargo\s*(入门|教程)"
                     r"|C\+\+\s*(面试题|基础教程|语法)|指针(详解|入门)",
        "unless": r"对比|选型|替代|迁移|性能|内存安全|所有权|borrow|重写",
        "why": "「Rust 入门」与「C++ 教程」是学习材料，不构成立场表达",
    },
}

_GUARD_CACHE: dict[str, tuple] = {}


def _guard_re(issue_id: str) -> tuple:
    if issue_id not in _GUARD_CACHE:
        g = TOPIC_GUARD.get(issue_id)
        if not g:
            _GUARD_CACHE[issue_id] = (None, None)
        else:
            _GUARD_CACHE[issue_id] = (
                re.compile(g["off_topic"], re.I) if g.get("off_topic") else None,
                re.compile(g["unless"], re.I) if g.get("unless") else None,
            )
    return _GUARD_CACHE[issue_id]


def is_off_topic(issue_id: str, text: str, title: str = "") -> bool:
    """这条内容是不是"字面命中但根本不是这场争论"。True = 该拦。

    两级判断，顺序很重要：
      ① **标题级**：标题里出现 off_topic 且没有任何 unless 词 → 直接拦。
         标题宣告了这篇文章在谈什么，它比正文更有决定性。
         （实测教训：「Python 里的"鸭子类型"」正文里顺嘴提了一句"类型检查"，
           只看正文就会放行 —— 必须让标题说了算。）
      ② **正文级**：正文命中 off_topic 且没有 unless 词 → 拦。
         这是兜底，拦标题正常、但内容是另一种东西的情况。
    """
    off, unless = _guard_re(issue_id)
    if off is None:
        return False
    if title and off.search(title):
        if not (unless is not None and unless.search(title)):
            return True
    if text and off.search(text):
        return not (unless is not None and unless.search(text))
    return False


def guard_why(issue_id: str) -> str:
    return (TOPIC_GUARD.get(issue_id) or {}).get("why", "")

