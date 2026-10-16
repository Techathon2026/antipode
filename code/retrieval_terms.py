"""议题 → 检索术语表

【这张表是 2026-10-16 实测出来的，不是拍脑袋写的】

反引擎要去找"与库条目相反的东西"，但**不能搜索反义词**（搜"微服务 不好"什么都搜不到）。
正确做法：搜**对立面自己的名字**。

实测记录（详见 docs/反仓库检索_实测记录.md）：
    ✅ 搜 `monolith`（HN 文章标题）→ 一排名篇，全是反对微服务的：
       "Goodbye Microservices"、"Monoliths Are the Future"、"Monolith First"、
       "Don't start with microservices – monoliths are your friend"
       —— 对立面的名字本身就是最好的检索词
    ✅ 搜 `"premature microservices"`（精确短语）→ 精确度 100%，但只有 1 条（召回≈0）
    ❌ 搜 `"we moved back to a monolith"` → 0 条
    ❌ 长短语（4 词以上）在 GitHub 会被拆成 AND 条件 → 捞回 33KB 的技术草稿
    ❌ HN 评论区搜索不能做召回 → Algolia 做前缀匹配，"monolithic"/"monolithically"
       全被算命中，结果里混进 2015 年的招聘帖

→ 结论：**用短词（1-2 个词）做召回，用模型做精确度。**
   检索阶段宁可多召回一些噪声，也别漏；筛掉噪声是 stance_extractor 的活。

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
    · 术语一律 1-2 个词，禁止长短语（会让召回归零）
"""

TERMS_VERSION = "v1-实测修正"

# 这两个议题不做检索：一个是纯梗（Tab vs 空格），一个是生活偏好（豆腐脑）。
# 它们的反信息由人工预置，用来演示 counter_type = 视野缺失(C) 那一类。
NO_RETRIEVAL = ("tabs_vs_spaces", "tofu_pudding")

ISSUE_TERMS: dict[str, dict] = {
    "microservices_vs_monolith": {
        "topics": ["microservices", "monolith", "微服务", "单体"],
        "sides": {
            "微服务": ["microservices", "微服务"],
            "单体": ["monolith", "单体"],
        },
        "retrieval": "auto",
    },
    "tdd_necessity": {
        "topics": ["TDD", "test driven", "测试驱动"],
        "sides": {
            "先写测试": ["TDD", "test driven", "测试先行"],
            "先写实现": ["TDD is dead", "no tests", "先写实现"],
        },
        "retrieval": "auto",
    },
    "monorepo_vs_multirepo": {
        "topics": ["monorepo", "multirepo", "单仓"],
        "sides": {
            "单体仓库": ["monorepo", "单仓"],
            "多仓库": ["multirepo", "multiple repos", "多仓"],
        },
        "retrieval": "auto",
    },
    "react_vs_vue": {
        "topics": ["react", "vue"],
        "sides": {
            "React": ["react", "reactjs"],
            "Vue": ["vue", "vuejs", "vue3"],
        },
        "retrieval": "auto",
    },
    "rest_vs_graphql": {
        "topics": ["graphql", "rest api"],
        "sides": {
            "REST": ["REST API", "RESTful"],
            "GraphQL": ["graphql"],
        },
        "retrieval": "auto",
    },
    "orm_or_raw_sql": {
        "topics": ["ORM", "raw SQL"],
        "sides": {
            "ORM": ["ORM", "object relational"],
            "手写 SQL": ["raw SQL", "native SQL", "手写SQL"],
        },
        "retrieval": "auto",
    },
    "typescript_strictness": {
        "topics": ["typescript strict", "type safety"],
        "sides": {
            "开严格模式": ["typescript strict", "strict mode"],
            "关严格模式": ["type gymnastics", "any type", "类型体操"],
        },
        "retrieval": "auto",
    },
    "oop_vs_fp": {
        "topics": ["functional programming", "object oriented"],
        "sides": {
            "面向对象": ["object oriented", "OOP"],
            "函数式": ["functional programming", "immutability"],
        },
        "retrieval": "auto",
    },
    "ai_assistant_weakens_dev": {
        "topics": ["copilot", "AI coding", "AI 编程"],
        "sides": {
            "AI 助手有害": ["copilot problem", "AI dependency", "AI 编程 依赖"],
            "AI 助手有益": ["copilot productivity", "AI coding speed"],
        },
        "retrieval": "auto",
    },
    "remote_vs_onsite": {
        "topics": ["remote work", "return to office", "远程办公"],
        "sides": {
            "远程办公": ["remote work", "work from home", "远程办公"],
            "坐班": ["return to office", "RTO", "back to office"],
        },
        "retrieval": "auto",
    },
    "rust_vs_cpp": {
        "topics": ["rust", "C++"],
        "sides": {
            "Rust": ["rust lang", "rustlang"],
            "C++": ["C++", "cpp"],
        },
        "retrieval": "auto",
    },
    "cloud_vs_selfhost": {
        "topics": ["cloud cost", "self hosting", "上云"],
        "sides": {
            "上云": ["cloud migration", "AWS"],
            "自建": ["self hosting", "self-hosted", "自建"],
        },
        "retrieval": "auto",
    },
    "code_review_strictness": {
        "topics": ["code review"],
        "sides": {
            "严格评审": ["code review", "review standards"],
            "宽松评审": ["code review waste", "no code review"],
        },
        "retrieval": "auto",
    },
    "static_vs_dynamic_typing": {
        "topics": ["static typing", "dynamic typing"],
        "sides": {
            "静态类型": ["static typing", "type safety"],
            "动态类型": ["dynamic typing", "duck typing"],
        },
        "retrieval": "auto",
    },
    "tabs_vs_spaces": {
        "topics": ["tabs", "spaces"],
        "sides": {
            "Tab": ["tabs"],
            "空格": ["spaces"],
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
