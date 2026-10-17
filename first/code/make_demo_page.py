#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「反库演示页」—— 把仓库条目和反库条目并排摆出来，现场可点、可见。

用途：
  1. 现场 Demo：评委不需要看命令行，直接看「左边是用户收的，右边是系统补的」
  2. 视频录制：静态页不依赖网络，录屏不会因为 API 抖动翻车
  3. 答辩举证：每条反库都能点回原文，页面里写明「全部来自真实检索，0 条合成」

用法：
  python code/make_demo_page.py                       # 默认读 data/vault_demo.*
  python code/make_demo_page.py --vault data/vault_url_only.json
  python code/make_demo_page.py --out docs/反库演示.html

零第三方依赖；产物是单文件 HTML，数据内嵌，断网也能打开。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 归宿 → 给评委看的一句话（不写"失败"，写"为什么这条没有反信息"）
OUTCOME_TEXT = {
    "neutral": ("原文没有明确立场", "这篇文章是中立讨论或综述，两边都提到但不站边。没有立场就没有对立面。"),
    "no_issue": ("议题不在支持清单内", "文章谈的不是我们建了二元议题的那 16 个争论，硬找对立面只会跑偏。"),
    "no_text": ("取不到正文", "只有链接、站点反爬或需要 JS 渲染。宁可不做，也不编造摘要。"),
    "no_candidate": ("检索没有召回候选", "换词、换源都没有找到同一场争论里的另一方。"),
    "all_rejected": ("候选全部被闸门拒掉", "召回到了东西，但过不了对立性 / 立场 / 观点性 / 议题守卫——不凑数。"),
    "error": ("这一条出错", "单条失败不影响整库，错误会单独记账。"),
    "disabled": ("该库没有开启反推荐", "反推荐是可选开关，按库开启。"),
    "truncated": ("候选过多，已截断", "只判前 N 条，避免单条拖垮整库。"),
}


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _issue_labels() -> dict[str, str]:
    try:
        sys.path.insert(0, str(ROOT / "code"))
        from stance_extractor import ISSUE_TAXONOMY  # type: ignore

        return {t["issue_id"]: t["label"] for t in ISSUE_TAXONOMY}
    except Exception:
        return {}


def _domain(url: str) -> str:
    u = (url or "").strip()
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
            break
    if u.startswith("www."):
        u = u[4:]
    return u.split("/")[0] or "未知来源"


def build_payload(vault_path: Path) -> dict:
    vault = _read_json(vault_path, {})
    vault_id = vault.get("vault_id") or vault_path.stem.replace(".items", "")
    stem = vault_path.name.replace(".items.jsonl", "").replace(".json", "")

    items = _read_jsonl(vault_path.parent / f"{stem}.items.jsonl")
    anti = _read_jsonl(vault_path.parent / f"antipode_{vault_id}.jsonl")
    stats = _read_json(vault_path.parent / f"antipode_{vault_id}.stats.json", {})
    labels = _issue_labels()

    by_item: dict[str, list[dict]] = {}
    for a in anti:
        by_item.setdefault(str(a.get("for_vault_item") or ""), []).append(a)

    stat_rows = {str(r.get("item_id")): r for r in (stats.get("rows") or [])}

    groups = []
    for it in items:
        iid = str(it.get("id") or "")
        row = stat_rows.get(iid, {})
        out = []
        for a in by_item.get(iid, []):
            out.append(
                {
                    "title": a.get("title") or "",
                    "url": a.get("url") or "",
                    "evidence_url": a.get("evidence_url") or a.get("url") or "",
                    "domain": _domain(a.get("evidence_url") or a.get("url") or ""),
                    "takes_side": a.get("takes_side") or "",
                    "summary": a.get("summary") or "",
                    "evidence": (a.get("evidence_span") or a.get("raw_excerpt") or "")[:600],
                    "score": a.get("opposition_score"),
                    "counter_type": a.get("counter_type") or "",
                    "synthesized": bool(a.get("synthesized")),
                    "stage": ((a.get("found_by") or {}).get("stage") or ""),
                    "source": ((a.get("found_by") or {}).get("source") or ""),
                }
            )
        out.sort(key=lambda x: (x["score"] or 0), reverse=True)

        outcome = row.get("outcome") or ("produced" if out else "")
        reason_title, reason_body = OUTCOME_TEXT.get(outcome, ("", ""))
        groups.append(
            {
                "id": iid,
                "title": it.get("title") or (_domain(it.get("url", "")) + " 的文章"),
                "url": it.get("url") or "",
                "domain": _domain(it.get("url") or ""),
                "summary": (it.get("summary") or "")[:220],
                "supports": it.get("supports") or "",
                "issue_id": it.get("issue_id") or "",
                "issue_label": labels.get(it.get("issue_id") or "", it.get("issue_id") or ""),
                "outcome": outcome,
                "reason_title": reason_title,
                "reason_body": reason_body,
                "rejected": (row.get("rejected") or {}),
                "candidates": row.get("candidates"),
                "anti": out,
            }
        )

    prod_groups = [g for g in groups if g["anti"]]
    total_anti = sum(len(g["anti"]) for g in groups)
    synthesized = sum(1 for g in groups for a in g["anti"] if a["synthesized"])

    return {
        "vault_id": vault_id,
        "topic": vault.get("topic") or "",
        "owner": vault.get("owner") or "",
        "enabled": bool(vault.get("antipode_enabled", True)),
        "ran_at": stats.get("ran_at") or "",
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        "groups": groups,
        "kpi": {
            "items": len(groups),
            "with_output": len(prod_groups),
            "anti": total_anti,
            "synthesized": synthesized,
            "candidates": sum(g["candidates"] or 0 for g in groups),
            "hit_rate": (round(len(prod_groups) / len(groups), 3) if groups else 0),
        },
    }


CSS = """
  :root{
    --bg:#f6f7f9; --card:#fff; --ink:#16181d; --dim:#5b6270; --line:#e5e7eb;
    --accent:#2f5bea; --accent2:#0f9d76; --warn:#c2410c; --soft:#eef2ff;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--ink);
       font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{max-width:1180px;margin:0 auto;padding:34px 26px 80px}
  header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;flex-wrap:wrap}
  .kicker{color:var(--accent);font-weight:700;letter-spacing:.14em;font-size:12px}
  h1{font-size:30px;line-height:1.25;margin:6px 0 6px;letter-spacing:-.02em}
  h1 small{font-weight:400;color:var(--dim);font-size:15px;letter-spacing:0;margin-left:10px}
  .lede{color:var(--dim);max-width:720px;margin:0}

  .switch{display:flex;align-items:center;gap:10px;background:var(--card);
          border:1px solid var(--line);border-radius:999px;padding:8px 14px 8px 12px;
          cursor:pointer;user-select:none;white-space:nowrap;font-size:13.5px}
  .switch .track{width:40px;height:22px;border-radius:999px;background:var(--accent);
                 position:relative;transition:background .18s}
  .switch .knob{position:absolute;top:2px;left:20px;width:18px;height:18px;border-radius:50%;
                background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);transition:left .18s}
  body.off .switch .track{background:#c7ccd6}
  body.off .switch .knob{left:2px}
  .switch b{font-weight:650}

  .kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:24px 0 6px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:12px 14px}
  .kpi .n{font-size:22px;font-weight:750;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
  .kpi .l{color:var(--dim);font-size:12px}

  .cols{display:grid;grid-template-columns:1fr 44px 1fr;gap:0;margin-top:22px}
  .colhead{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0 10px;
           display:flex;align-items:baseline;gap:8px}
  .colhead h2{font-size:15px;margin:0;letter-spacing:.02em}
  .colhead .sub{color:var(--dim);font-size:12px}
  .colhead.anti h2{color:var(--accent2)}
  .linkcol{position:relative}
  .linkcol::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;
                   background:var(--line);transform:translateX(-50%)}

  .group{display:contents}
  .pair{display:grid;grid-template-columns:1fr 44px 1fr;margin-bottom:16px;padding-bottom:16px}
  .pair + .pair{border-top:1px dashed var(--line);padding-top:18px}
  .cell{min-width:0}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:14px 16px}
  .cell:not(.anti) .card{height:auto}
  .card .t{font-weight:700;font-size:15px;line-height:1.45;margin:0 0 6px}
  .card .t a{color:var(--ink);text-decoration:none}
  .card .t a:hover{color:var(--accent);text-decoration:underline}
  .meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px;
        color:var(--dim);font-size:12px}
  .tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
       font-weight:650;white-space:nowrap;background:#f3f4f6;color:#374151}
  .tag.a{background:#eef2ff;color:var(--accent)}
  .tag.b{background:#e8f5ee;color:var(--accent2)}
  .tag.w{background:#fff5ed;color:var(--warn)}
  .sum{color:#3c4250;font-size:13.5px;margin:0}
  .arrow{align-self:center;text-align:center;color:#b6bcc8;font-size:13px}
  .arrow span{display:inline-block;background:var(--bg);padding:6px 0;position:relative;z-index:1}

  .antiwrap{display:flex;flex-direction:column;gap:10px}
  .anti .t{font-size:14px}
  .ev{margin-top:9px;padding:9px 11px;background:#fafbfc;border:1px solid #eef0f3;
      border-left:3px solid var(--accent2);border-radius:8px;color:#414855;font-size:12.5px;
      max-height:118px;overflow:auto;white-space:pre-wrap}
  .ev b{display:block;color:var(--dim);font-weight:600;font-size:11px;letter-spacing:.06em;
        margin-bottom:3px}
  .gobtn{display:inline-flex;align-items:center;gap:6px;margin-top:9px;padding:5px 11px;
         border-radius:8px;background:#f5f7ff;color:var(--accent);text-decoration:none;
         font-size:12.5px;font-weight:650;border:1px solid #e2e8fb}
  .gobtn:hover{background:#eaefff}
  .score{font-variant-numeric:tabular-nums;font-weight:700;color:var(--accent2)}

  .empty{background:#fbfbfc;border:1px dashed #dfe3ea;border-radius:12px;padding:14px 16px;
         color:var(--dim);font-size:13px;height:100%}
  .empty .et{color:#3c4250;font-weight:650;font-size:13.5px;margin-bottom:4px}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .chip{background:#f3f4f6;color:#5b6270;border-radius:6px;padding:1px 7px;font-size:11.5px}

  body.off .cell.anti{opacity:.28;filter:grayscale(1);pointer-events:none}
  .offnote{display:none;margin:26px 0 0;padding:16px 18px;border-radius:12px;
           background:#fff8f1;border:1px solid #f6ddc4;color:#8a4b18;font-size:13.5px}
  body.off .offnote{display:block}

  footer{margin-top:34px;color:var(--dim);font-size:12.5px;border-top:1px solid var(--line);
         padding-top:14px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
  .foot-note{max-width:760px}
  .foot-note b{color:var(--ink)}
  @media (max-width:900px){
    .cols,.pair{grid-template-columns:1fr}
    .linkcol{display:none}
    .kpis{grid-template-columns:repeat(2,1fr)}
    .arrow{display:none}
  }
  @media print{body{background:#fff}.card,.empty{break-inside:avoid}}
"""


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render(payload: dict) -> str:
    k = payload["kpi"]
    parts: list[str] = []

    for g in payload["groups"]:
        # 左：仓库条目
        left = [
            '<div class="card">',
            f'<p class="t"><a href="{_esc(g["url"])}" target="_blank" rel="noopener">{_esc(g["title"])}</a></p>',
            '<div class="meta">',
            f'<span class="tag a">仓库条目</span>',
            f'<span class="tag">支持 · {_esc(g["supports"] or "—")}</span>',
            f'<span>{_esc(g["domain"])}</span>',
            f'<span>{_esc(g["issue_label"])}</span>',
            "</div>",
            f'<p class="sum">{_esc(g["summary"] or "（原条目未附摘要）")}</p>',
            "</div>",
        ]

        # 右：反库条目 or 空状态
        right: list[str] = []
        if g["anti"]:
            right.append('<div class="antiwrap">')
            for a in g["anti"]:
                url = a["evidence_url"] or a["url"]
                score = a["score"]
                score_txt = f'{score:.2f}' if isinstance(score, (int, float)) else "—"
                stage_txt = {"origin": "原站内", "global": "全网", "synthesized": "AI 自编"}.get(
                    a["stage"], a["stage"] or "检索"
                )
                right.append('<div class="card anti">')
                right.append(
                    f'<p class="t"><a href="{_esc(a["url"])}" target="_blank" rel="noopener">{_esc(a["title"])}</a></p>'
                )
                right.append('<div class="meta">')
                right.append(f'<span class="tag b">反库条目</span>')
                right.append(f'<span class="tag">站 · {_esc(a["takes_side"])}</span>')
                right.append(f'<span>对立度 <span class="score">{score_txt}</span></span>')
                right.append(f'<span>{_esc(a["domain"])}</span>')
                right.append(f'<span>{_esc(stage_txt)}</span>')
                if a["synthesized"]:
                    right.append('<span class="tag w">AI 自编（默认关闭）</span>')
                right.append("</div>")
                if a["summary"]:
                    right.append(f'<p class="sum">{_esc(a["summary"])}</p>')
                if a["evidence"]:
                    right.append(
                        f'<div class="ev"><b>原文证据</b>{_esc(a["evidence"])}</div>'
                    )
                right.append(
                    f'<a class="gobtn" href="{_esc(url)}" target="_blank" rel="noopener">回现场 ↗</a>'
                )
                right.append("</div>")
            right.append("</div>")
        else:
            chips = "".join(
                f'<span class="chip">{_esc(k2)} · {v}</span>' for k2, v in (g["rejected"] or {}).items()
            )
            right.append('<div class="empty">')
            right.append(f'<div class="et">这一条没有反信息：{_esc(g["reason_title"] or "未产出")}</div>')
            right.append(f'<div>{_esc(g["reason_body"])}</div>')
            if chips:
                right.append(f'<div class="chips">{chips}</div>')
            right.append("</div>")

        n = len(g["anti"])
        arrow = "→" if n <= 1 else f"→<br><small>×{n}</small>"
        parts.append(
            '<div class="pair">'
            f'<div class="cell">{"".join(left)}</div>'
            f'<div class="arrow"><span>{arrow}</span></div>'
            f'<div class="cell anti">{"".join(right)}</div>'
            "</div>"
        )

    switch_label = "反推荐：开" if payload["enabled"] else "反推荐：关"
    body_class = "" if payload["enabled"] else ' class="off"'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Antipode 对跖点 · 反库演示</title>
<style>{CSS}</style>
</head>
<body{body_class}>
<div class="wrap">
  <header>
    <div>
      <div class="kicker">TECHATHON 2026 · 反仓库演示</div>
      <h1>Antipode 对跖点<small>左边是你收的，右边是你没看见的</small></h1>
      <p class="lede">
        库「{_esc(payload["topic"] or payload["vault_id"])}」里的每一条，
        系统都去找同一场争论里的<b>另一面</b>，找到就放进反库并附原文出处；
        找不到就如实说为什么找不到。
      </p>
    </div>
    <div class="switch" id="sw" role="switch" aria-checked="{str(payload["enabled"]).lower()}">
      <span class="track"><span class="knob"></span></span>
      <b>{switch_label}</b>
    </div>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="n">{k["items"]}</div><div class="l">仓库条目</div></div>
    <div class="kpi"><div class="n">{k["with_output"]}</div><div class="l">找到对立面</div></div>
    <div class="kpi"><div class="n">{k["anti"]}</div><div class="l">反库条目</div></div>
    <div class="kpi"><div class="n">{k["candidates"]}</div><div class="l">检索候选</div></div>
    <div class="kpi"><div class="n">{k["synthesized"]}</div><div class="l">AI 自编条目</div></div>
  </div>

  <div class="offnote">
    这个库没有开启反推荐 —— 反推荐是按库开启的可选能力。邮件、代码 issue 这类
    「私人事务 / 工作材料」本就没有对立面，不该开；观点类文章才开。
  </div>

  <div class="cols">
    <div class="colhead"><h2>仓库</h2><span class="sub">用户关注的、已省流的内容</span></div>
    <div></div>
    <div class="colhead anti"><h2>反仓库</h2><span class="sub">同议题、反立场、带原文出处</span></div>
  </div>

  {''.join(parts)}

  <footer>
    <div class="foot-note">
      口径：以上反库条目由 <b>run_vault.py</b> 于 {_esc(payload["ran_at"] or "—")} 实跑产出，
      共 {k["anti"]} 条，<b>全部带真实 URL 与原文证据片段</b>，
      其中 AI 自编 {k["synthesized"]} 条（第三档，默认关闭）。
      「没有反信息」不是失败，是判定结果 —— 强行凑数才叫失败。<br>
      说明：左侧仓库条目为<b>演示用构造数据</b>（模拟队友侧的库），
      右侧反库条目为<b>真实检索产出</b>，链接可点开核对。
    </div>
    <div>生成于 {_esc(payload["generated_at"])} · 纯静态，断网可用</div>
  </footer>
</div>
<script>
  document.getElementById('sw').addEventListener('click', function () {{
    var on = document.body.classList.toggle('off') === false;
    this.setAttribute('aria-checked', String(on));
    this.querySelector('b').textContent = on ? '反推荐：开' : '反推荐：关';
  }});
</script>
</body>
</html>
"""
    return html


def _resolve(p) -> Path:
    """相对路径先按项目根找、再按当前目录找。

    这样「cd first && python code/make_demo_page.py」和
    「在仓库根 python first/code/make_demo_page.py」两种用法都不会把路径拼错。
    """
    q = Path(p)
    if q.is_absolute():
        return q
    for base in (ROOT, Path.cwd()):
        cand = base / q
        if cand.exists():
            return cand
    return ROOT / q


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成反库演示页")
    ap.add_argument("--vault", default=str(ROOT / "data" / "vault_demo.json"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "反库演示.html"))
    args = ap.parse_args(argv)

    vault_path = _resolve(args.vault)
    if not vault_path.exists():
        print(f"[x] 找不到库文件：{vault_path}")
        return 2

    payload = build_payload(vault_path)
    if not payload["groups"]:
        print(f"[x] 库里没有条目（{vault_path.name} 对应的 .items.jsonl 为空）")
        return 2

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(payload), encoding="utf-8")

    k = payload["kpi"]
    print(f"[✓] {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    print(f"    仓库条目 {k['items']} / 有对立面 {k['with_output']} / 反库条目 {k['anti']}"
          f" / 候选 {k['candidates']} / 自编 {k['synthesized']}")
    if k["synthesized"]:
        print("    [warn] 页面里含 synthesized 条目，确认演示时是否需要展示")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
