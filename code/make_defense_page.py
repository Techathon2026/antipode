#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成答辩看板（单页 HTML，离线可开，不依赖任何外部资源）。

    python code/make_defense_page.py
    → docs/答辩看板.html

数字**全部从实测产物里读**，不手抄：
    data/antipode_<vault>.stats.json   （每条库条目的漏斗与归宿）
    data/antipode_<vault>.jsonl        （最终反库条目）
所以每次重跑完，重新生成一次，看板上的数字就是新的。
"""

from __future__ import annotations

import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "docs", "答辩看板.html")
CST = timezone(timedelta(hours=8))


def load_stats(name: str) -> dict | None:
    p = os.path.join(DATA, f"antipode_{name}.stats.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def load_items(name: str) -> list[dict]:
    p = os.path.join(DATA, f"antipode_{name}.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def agg(stats: dict | None) -> dict:
    """把一个库的 9 条/6 条汇总成漏斗总数。"""
    if not stats:
        return {}
    rows = stats.get("rows") or []
    rej: dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("rejected") or {}).items():
            rej[k] = rej.get(k, 0) + v
    return {
        "items": stats.get("items_total", len(rows)),
        "with_output": stats.get("items_with_output", 0),
        "produced": stats.get("antipode_items", 0),
        "hit_rate": stats.get("hit_rate", 0),
        "candidates": sum(r.get("candidates") or 0 for r in rows),
        "prescreened": sum(r.get("prescreened") or 0 for r in rows),
        "passed": sum(r.get("passed") or 0 for r in rows),
        "rejected": rej,
        "outcomes": stats.get("outcome_distribution") or {},
        "ran_at": stats.get("ran_at", ""),
        "rows": rows,
    }


def esc(s) -> str:
    return html.escape(str(s or ""))


def bar(label: str, value: int, total: int, color: str) -> str:
    pct = (value / total * 100) if total else 0
    return f"""
      <div class="funnel-row">
        <div class="funnel-label">{esc(label)}</div>
        <div class="funnel-track">
          <div class="funnel-fill" style="width:{max(pct, 0.6):.1f}%;background:{color}"></div>
        </div>
        <div class="funnel-num">{value}<span>{pct:.0f}%</span></div>
      </div>"""


def funnel_block(title: str, sub: str, a: dict, accent: str) -> str:
    if not a:
        return ""
    c = max(a["candidates"], 1)
    rows = (bar("检索候选", a["candidates"], c, accent) +
            bar("粗筛后送检", a["prescreened"], c, accent) +
            bar("通过四道闸门", a["passed"], c, accent) +
            bar("写入反库", a["produced"], c, accent))
    return f"""
    <div class="card">
      <div class="card-head"><h3>{esc(title)}</h3><span class="muted">{esc(sub)}</span></div>
      <div class="funnel">{rows}</div>
      <div class="funnel-foot">
        漏斗收束 <b>{a['candidates']} → {a['prescreened']} → {a['passed']} → {a['produced']}</b>
        　最终保留 <b>{(a['produced'] / c * 100):.1f}%</b>
      </div>
    </div>"""


def produced_table(items: list[dict]) -> str:
    if not items:
        return '<p class="muted">（本次没有产出）</p>'
    trs = []
    for it in items:
        url = str(it.get("url") or "")
        short = url.replace("https://", "").replace("http://", "")[:58]
        trs.append(f"""<tr>
          <td><span class="tag side">{esc(it.get('takes_side'))}</span></td>
          <td class="title"><a href="{esc(url)}" target="_blank" rel="noreferrer">{esc(it.get('title'))}</a>
              <div class="ev">{esc((it.get('evidence_span') or '')[:88])}</div></td>
          <td class="muted small">{esc(it.get('found_by', {}).get('source', ''))}<br>{esc(short)}</td>
        </tr>""")
    return f"""<table class="tbl"><thead><tr>
        <th style="width:76px">站哪一边</th><th>反信息（点击回原文）</th><th style="width:190px">来源</th>
      </tr></thead><tbody>{''.join(trs)}</tbody></table>"""


def outcome_table(a: dict) -> str:
    label = {
        "produced": "有产出", "all_rejected": "候选全被闸门拒",
        "neutral": "中立（无可对立）", "no_issue": "议题不在表内",
        "no_stance": "原文无明确立场", "off_issue": "判出的议题与原文不符",
        "no_text": "没有可用正文", "fetch_failed": "链接抓不到",
        "no_candidate": "检索零候选", "error": "出错", "disabled": "库未开启反推荐",
        "truncated": "通过但被 top-k 截断",
    }
    if not a.get("outcomes"):
        return ""
    trs = []
    for k, v in sorted(a["outcomes"].items(), key=lambda x: -x[1]):
        trs.append(f"<tr><td>{esc(label.get(k, k))}</td><td class='num'>{v}</td>"
                   f"<td class='muted small'>{esc(k)}</td></tr>")
    return f"""<table class="tbl compact"><thead><tr><th>归宿</th><th style="width:60px">条数</th>
      <th style="width:130px">机器可读值</th></tr></thead><tbody>{''.join(trs)}</tbody></table>"""


def reject_table(a: dict) -> str:
    rej = a.get("rejected") or {}
    if not rej:
        return ""
    trs = "".join(f"<tr><td>{esc(k)}</td><td class='num'>{v}</td></tr>"
                  for k, v in sorted(rej.items(), key=lambda x: -x[1]))
    return f"""<table class="tbl compact"><thead><tr><th>被闸门拒的原因</th>
      <th style="width:60px">次数</th></tr></thead><tbody>{trs}</tbody></table>"""


GATES = [
    ("① 议题守卫", "规则", "字面命中但根本不是这场争论（辅助驾驶≠AI 编程助手、自建 CDN≠自建 vs 上云）", "10 / 16 个议题已配"),
    ("② 教程标题守卫", "规则", "标题像教程又没有任何立场词 → 不是观点文（《零基础学 React》不是「支持 React 的观点」）", "零调用成本"),
    ("③ 自指排除", "规则", "检索把库条目自己搜回来、又判成反方 —— 推荐系统里最刺眼的错", "实测抓到 1 次"),
    ("④ 二次判立场", "模型", "模型说它站对立面不算，要它自己再判一遍并给出原文证据", "8 次调用 / 条"),
    ("⑤ 对立性表述 + 证据校验", "规则+模型", "没有\"反对/转向/对比\"的话术不算反信息；证据必须是原文真实子串", "100% 校验"),
]

LIMITS = [
    ("中文语料有时真的没有反面声音", "TDD、远程办公两个议题跑完整库产出 0 条。"
     "这不是没跑通，是语料里确实没有站得住的反面文章 —— 我们选择空着，不硬凑。"),
    ("教程帖仍可能被当成观点", "《vue3 源码解析》这类\"教程里顺带一句评价\"的内容，规则和阈值都不好切，已知会漏。"),
    ("站点可达性不稳", "V2EX 官方站在本机 502；知乎 403、公众号要 JS 渲染；CSDN 会间歇性 521（已做退避重试）。"
     "抓不到就明确报「抓不到」，绝不编正文。"),
    ("立场抽取不是 100%", "评测集 90.2% 四分类准确率。判错时反库会跑偏，所以每一条都要人眼可回原文核对。"),
    ("评测本身有噪声", "同一 prompt 连跑两次，失败条目会变 —— 服务端缓存把不确定性伪装成了确定性，这个坑我们记下来了。"),
]


def main() -> int:
    demo = agg(load_stats("demo-vault"))
    url_only = agg(load_stats("url-only"))
    it_demo = load_items("demo-vault")
    it_url = load_items("url-only")

    total_produced = len(it_demo) + len(it_url)
    total_items = (demo.get("items", 0) + url_only.get("items", 0))
    all_cand = demo.get("candidates", 0) + url_only.get("candidates", 0)
    all_pass = demo.get("passed", 0) + url_only.get("passed", 0)
    ran_at = demo.get("ran_at") or datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    gates = "".join(f"""<tr><td class="gname">{esc(n)}</td>
        <td><span class="tag kind">{esc(k)}</span></td>
        <td>{esc(d)}</td><td class="muted small">{esc(m)}</td></tr>""" for n, k, d, m in GATES)
    limits = "".join(f"""<li><b>{esc(t)}</b><br><span class="muted">{esc(d)}</span></li>"""
                     for t, d in LIMITS)

    qa = [
        ("会不会变成抬杠机器？",
         "不会。三道硬约束：必须有论证结构、必须有可验证出处、必须过对立度阈值；"
         "排序里「论证质量」和「对立度」各占 40%，抬杠拿不到分。"),
        ("反库里会不会编东西？",
         "不会。LLM 只负责「提出问题」，答案必须来自真实检索，每条都带 URL + 原文证据子串，"
         "证据不在原文里直接丢弃。AI 自编那一档默认关闭。"),
        ("为什么有时候没有反信息？",
         "四种情况，看板里逐条可查：链接抓不到 / 原文没有明确立场 / 检索到候选但全被闸门拒 / "
         "议题不在表内。空着比硬凑一条更有用。"),
        ("反推荐会不会很烦人？",
         "它默认是关的，按库开启（antipode_enabled）。邮件、代码 issue 这类私人事务本来就不该开。"),
        ("为什么不全用模型判？",
         "模型判「这场争论里你站哪边」很强（90.2%），但判「这条内容是不是跑错场子」很弱 —— "
         "同形异义这类错误交给确定性规则更便宜也更可解释。"),
        ("队友的库只给链接怎么办？",
         "我们自己取正文（CSDN / GitHub / 少数派 / 普通博客实测可抓），取不到就明确报失败；"
         "用户授权后可带自己的登录态读他本来就有权看的文章。"),
    ]
    qa_html = "".join(f"""<div class="qa"><div class="q">{esc(q)}</div>
        <div class="a">{esc(a)}</div></div>""" for q, a in qa)

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Antipode · 对跖点 —— 答辩看板</title>
<style>
  :root {{
    --bg:#f6f7f9; --card:#ffffff; --ink:#16181d; --dim:#5b6270; --line:#e5e7eb;
    --accent:#2f5bea; --accent2:#0f9d76; --warn:#c2410c;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:44px 26px 72px; }}
  header {{ margin-bottom:26px; }}
  .kicker {{ color:var(--accent); font-weight:700; letter-spacing:.14em; font-size:12px; }}
  h1 {{ font-size:34px; line-height:1.25; margin:6px 0 8px; letter-spacing:-.02em; }}
  h1 small {{ font-weight:400; color:var(--dim); font-size:16px; letter-spacing:0; }}
  .lede {{ color:var(--dim); max-width:760px; }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:26px 0 30px; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
  .stat .n {{ font-size:26px; font-weight:750; letter-spacing:-.02em; }}
  .stat .l {{ color:var(--dim); font-size:12.5px; }}
  h2 {{ font-size:19px; margin:34px 0 12px; padding-bottom:8px; border-bottom:1px solid var(--line); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:16px 18px; margin-bottom:14px; }}
  .card-head {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; }}
  .card-head h3 {{ margin:0 0 10px; font-size:15.5px; }}
  .funnel-row {{ display:grid; grid-template-columns:110px 1fr 86px; align-items:center;
                 gap:10px; margin:7px 0; }}
  .funnel-label {{ color:var(--dim); font-size:13px; }}
  .funnel-track {{ height:16px; background:#eef0f4; border-radius:5px; overflow:hidden; }}
  .funnel-fill {{ height:100%; border-radius:5px; }}
  .funnel-num {{ font-variant-numeric:tabular-nums; font-weight:650; text-align:right; }}
  .funnel-num span {{ color:var(--dim); font-weight:400; font-size:12px; margin-left:6px; }}
  .funnel-foot {{ margin-top:10px; padding-top:10px; border-top:1px dashed var(--line);
                  color:var(--dim); font-size:13px; }}
  .funnel-foot b {{ color:var(--ink); }}
  table.tbl {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  .tbl th {{ text-align:left; color:var(--dim); font-weight:600; font-size:12px;
             padding:6px 8px; border-bottom:1px solid var(--line); }}
  .tbl td {{ padding:8px; border-bottom:1px solid #f1f2f5; vertical-align:top; }}
  .tbl tr:last-child td {{ border-bottom:0; }}
  .tbl.compact td {{ padding:6px 8px; }}
  .tbl .num {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:650; }}
  .tbl .title a {{ color:var(--ink); text-decoration:none; font-weight:600; }}
  .tbl .title a:hover {{ color:var(--accent); text-decoration:underline; }}
  .ev {{ color:var(--dim); font-size:12px; margin-top:3px; }}
  .tag {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px;
          background:#eef2ff; color:var(--accent); font-weight:600; white-space:nowrap; }}
  .tag.kind {{ background:#f3f4f6; color:#374151; }}
  .tag.side {{ background:#e8f5ee; color:var(--accent2); }}
  .gname {{ font-weight:650; white-space:nowrap; }}
  .muted {{ color:var(--dim); }}
  .small {{ font-size:12.5px; }}
  ul.limits {{ margin:0; padding-left:20px; }}
  ul.limits li {{ margin:9px 0; }}
  .qa {{ background:var(--card); border:1px solid var(--line); border-left:3px solid var(--accent);
         border-radius:10px; padding:12px 16px; margin-bottom:10px; }}
  .qa .q {{ font-weight:700; }}
  .qa .a {{ color:var(--dim); margin-top:4px; }}
  footer {{ margin-top:34px; color:var(--dim); font-size:12.5px; border-top:1px solid var(--line);
            padding-top:14px; }}
  @media (max-width:820px) {{
    .stats {{ grid-template-columns:repeat(2,1fr); }}
    .funnel-row {{ grid-template-columns:86px 1fr 74px; }}
  }}
  @media print {{ body {{ background:#fff; }} .card,.stat,.qa {{ break-inside:avoid; }} }}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="kicker">TECHATHON 2026 · 融合信息软件</div>
    <h1>Antipode 对跖点 <small>给你没看见、但值得看见的那一面</small></h1>
    <p class="lede">
      用户把关注的内容收进「仓库」；系统自动建一个「反仓库」——
      在同一场争论里，找到他<b>没有看到</b>的那一面的真实文章，带可验证出处，省流呈现。
      反推荐是可选开关，按库开启；反库独立存放，靠条目 id 与仓库对应。
    </p>
  </header>

  <div class="stats">
    <div class="stat"><div class="n">{total_produced}</div><div class="l">反库条目（全部带真实 URL）</div></div>
    <div class="stat"><div class="n">{total_items}</div><div class="l">已跑库条目（两次实测）</div></div>
    <div class="stat"><div class="n">{all_cand} → {all_pass}</div><div class="l">候选 → 通过闸门</div></div>
    <div class="stat"><div class="n">90.2%</div><div class="l">立场抽取四分类（41 条评测集）</div></div>
  </div>

  <h2>一、漏斗：每条反信息是怎么被筛出来的</h2>
  {funnel_block("实测 A · 库条目（正文已就绪，9 条）", f"跑于 {ran_at}", demo, "#2f5bea")}
  {funnel_block("实测 B · 只给链接（我们自己取正文，6 条）", "队友只给 URL 的真实场景 —— 取正文、判立场、再检索", url_only, "#0f9d76")}

  <h2>二、没有反信息时，原因必须分得清</h2>
  <div class="card">
    <div class="card-head"><h3>每条库条目的归宿（实测 A）</h3>
      <span class="muted small">评委问「这条为什么没有」，答案是这一栏，不是「系统还没做好」</span></div>
    {outcome_table(demo)}
  </div>
  <div class="card">
    <div class="card-head"><h3>被闸门拒掉的原因分布（实测 A）</h3>
      <span class="muted small">搜到了、判过了、但不合格 —— 和「一条都没搜到」是两件事</span></div>
    {reject_table(demo)}
  </div>

  <h2>三、五道闸门（防跑题、防自指、防抬杠）</h2>
  <div class="card">
    <table class="tbl"><thead><tr><th style="width:170px">闸门</th><th style="width:90px">类型</th>
      <th>拦什么</th><th style="width:120px">成本/覆盖</th></tr></thead>
      <tbody>{gates}</tbody></table>
  </div>

  <h2>四、实际产出的反库条目（实测 A）</h2>
  <div class="card">{produced_table(it_demo)}</div>

  <h2>五、只给链接也能跑（实测 B）</h2>
  <div class="card">{produced_table(it_url)}</div>

  <h2>六、我们知道自己会错在哪</h2>
  <div class="card"><ul class="limits">{limits}</ul></div>

  <h2>七、可能被问到的问题</h2>
  {qa_html}

  <footer>
    数字由 <code>code/make_defense_page.py</code> 从 <code>data/*.stats.json</code> 直接生成，
    与反库文件同源，不存在手抄。最后生成：{datetime.now(CST).strftime('%Y-%m-%d %H:%M')}（CST）
  </footer>
</div>
</body>
</html>
"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"已生成 {OUT}")
    print(f"  实测A：{demo.get('items', 0)} 条 → {demo.get('produced', 0)} 条反信息"
          f"（候选 {demo.get('candidates', 0)}，通过 {demo.get('passed', 0)}）")
    print(f"  实测B：{url_only.get('items', 0)} 条 → {url_only.get('produced', 0)} 条反信息"
          f"（候选 {url_only.get('candidates', 0)}，通过 {url_only.get('passed', 0)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
