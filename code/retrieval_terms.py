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
