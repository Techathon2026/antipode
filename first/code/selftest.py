#!/usr/bin/env python3
"""一条命令自检全链路 —— 现场出了问题先跑这个，别猜。

    python code/selftest.py           # 不联网、不调用模型，约 10 秒
    python code/selftest.py --live    # 额外真跑一条 URL（会花钱、要网络）

查七件事：
  1. 语法      每个 .py 能不能编译
  2. 依赖      有没有偷偷引入第三方包（项目承诺零依赖）
  3. 议题表    16 个二元议题是否都配了词、都能推出对立面
  4. 离线链路  engine.selftest()：库条目 → 反库条目 的十几项断言
  5. 采集      账号/链接 → 库：平台判定、选择器、浏览器、兜底逻辑
  6. HTTP      /health、/issues、坏请求是否按契约返回
  7. 产物      看板 / 演示页 / PPTX 在不在，是否比数据旧

退出码 0 = 全通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = CODE.parent
sys.path.insert(0, str(CODE))

OK, BAD, SKIP = "✅", "❌", "⏭"
results: list[tuple[bool | None, str, str]] = []


def add(ok: bool | None, name: str, note: str = "") -> None:
    results.append((ok, name, note))
    mark = SKIP if ok is None else (OK if ok else BAD)
    print(f"  {mark} {name}" + (f"   （{note}）" if note else ""))


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 66)


# ---------------------------------------------------------------------------
# 1. 语法
# ---------------------------------------------------------------------------
def check_syntax() -> None:
    import ast
    bad = []
    for p in sorted(CODE.glob("*.py")):
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{p.name}:{e.lineno}")
    add(not bad, f"{len(list(CODE.glob('*.py')))} 个 .py 文件语法正确",
        ", ".join(bad) if bad else "")


# ---------------------------------------------------------------------------
# 2. 依赖
# ---------------------------------------------------------------------------
STDLIB_HINT = {
    "openai", "requests", "numpy", "pandas", "httpx", "bs4",
    "lxml", "dotenv", "selenium", "playwright",
}


def check_deps() -> None:
    """项目承诺只用标准库。

    `openai` / `dotenv` 是**函数内延迟导入**的可选依赖（没装也能跑离线自检），
    所以只把「模块级 import」判为失败，函数内的记为可选。
    """
    import ast
    local = {p.stem for p in CODE.glob("*.py")}
    top: set[str] = set()
    lazy: set[str] = set()

    for p in CODE.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                if n in local or n in sys.stdlib_module_names or n.startswith("_"):
                    continue
                (top if node.col_offset == 0 else lazy).add(n)

    add(not top, "模块级只用标准库", "、".join(sorted(top)) if top else "零依赖")
    if lazy:
        add(None, "可选依赖（函数内延迟导入，不装也能跑）", "、".join(sorted(lazy)))


# ---------------------------------------------------------------------------
# 3. 议题表与词表
# ---------------------------------------------------------------------------
def check_taxonomy() -> None:
    from stance_extractor import ISSUE_TAXONOMY, opposite_side, resolve_side
    from retrieval_terms import ISSUE_TERMS, TOPIC_GUARD, NO_RETRIEVAL

    ids = [it["issue_id"] for it in ISSUE_TAXONOMY]
    add(len(ISSUE_TAXONOMY) == 16, "议题表 16 条", f"实际 {len(ISSUE_TAXONOMY)}")

    missing = [i for i in ids if i not in ISSUE_TERMS]
    add(not missing, "每个议题都有检索词", "、".join(missing) if missing else f"{len(ISSUE_TERMS)} 组")

    # 每个议题的两侧都要有词，否则检索会只搜到一边 —— 这是"偏袒"的根源
    no_side = [it["issue_id"] for it in ISSUE_TAXONOMY
               if set((ISSUE_TERMS.get(it["issue_id"]) or {}).get("sides") or {})
               != {it["side_a"], it["side_b"]}]
    add(not no_side, "两侧立场都配了词（防止只搜得到一边）",
        "、".join(no_side) if no_side else "16/16")

    broken = []
    for it in ISSUE_TAXONOMY:
        try:
            if resolve_side(it, it["side_b"]) != "b" or opposite_side(it, "a") != it["side_b"]:
                broken.append(it["issue_id"])
        except Exception as e:                                     # noqa: BLE001
            broken.append(f"{it['issue_id']}:{type(e).__name__}")
    add(not broken, "每个议题都能推出对立面", "、".join(broken) if broken else "16/16")

    stray = [k for k in TOPIC_GUARD if k not in ids]
    add(not stray, "议题守卫没有拼错的议题 ID", "、".join(stray) if stray else f"{len(TOPIC_GUARD)} 个议题设了守卫")
    add(all(k in ids for k in NO_RETRIEVAL), "「不可检索」名单也在议题表内", "、".join(NO_RETRIEVAL))


# ---------------------------------------------------------------------------
# 4. 离线链路
# ---------------------------------------------------------------------------
def check_engine() -> None:
    import antipode_engine as eng
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = eng.selftest()
    except Exception as e:                                         # noqa: BLE001
        add(False, "离线链路（engine.selftest）", f"{type(e).__name__}: {e}")
        return
    failed = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip().startswith(BAD)]
    add(code == 0, "离线链路（engine.selftest）",
        f"{len(failed)} 项未过" if failed else "全部断言通过")


# ---------------------------------------------------------------------------
# 5. 采集层（账号 / 链接 → 库）
# ---------------------------------------------------------------------------
def check_collect() -> None:
    import build_vault as bv
    import collect_account as ca

    # 平台判定：域名 → 平台，错了会拿错选择器，一条都抽不到
    cases = {
        "https://www.douyin.com/user/MS4w": "douyin",
        "https://weibo.com/u/123": "weibo",
        "https://github.com/ciqiongovo": "github",
        "https://space.bilibili.com/1": "bilibili",
        "https://blog.example.com/x": "generic",
    }
    wrong = [u for u, want in cases.items() if ca.detect_platform(u) != want]
    add(not wrong, "平台判定（抖音/微博/GitHub/B站/其它）",
        "、".join(wrong) if wrong else f"{len(cases)}/{len(cases)}")

    # 每个平台都要有专用选择器，否则全靠兜底，噪声会很大
    need = ("douyin", "weibo", "github", "bilibili", "generic")
    miss = [k for k in need if k not in ca.CONTENT_SELECTOR]
    add(not miss, "主要平台都有内容选择器", "、".join(miss) if miss else f"{len(ca.CONTENT_SELECTOR)} 个")

    # 风控识别：被拦时要能说清"是验证码，不是代码坏了"
    blocked = ca._block_reason("请完成安全验证后继续")
    clean = ca._block_reason("这是一篇正常的文章")
    add(bool(blocked) and not clean, "能识别平台风控页（验证码/异常流量）",
        f"{blocked or '未识别'} / 正常页{'误报' if clean else '不误报'}")

    # 浏览器：本机有没有可用的 Chromium
    try:
        from browser_cdp import find_browser, has_websockets
        exe = find_browser()
        add(bool(exe), "找到可用浏览器（Edge/Chrome）", os.path.basename(exe))
        has_ws = has_websockets()
        add(True if has_ws else None,
            "浏览器采集依赖 websockets",
            "已装" if has_ws else "未装：只有 --browser 这条路受影响")
    except Exception as e:                                     # noqa: BLE001
        add(False, "找到可用浏览器（Edge/Chrome）", f"{type(e).__name__}: {e}")

    # 列表页片段兜底：抓不到正文时，条目不能整个丢掉
    long_text = "这是一段已经从列表页拿到的正文。" * 30
    it = bv.build_item("https://example.invalid/x", None,
                       bv.make_opts(no_model=True), prefetched={"title": "示例", "text": long_text})
    ok = it.get("fetch_method") == "list_page" and not it.get("error") and it.get("title") == "示例"
    add(ok, "列表页片段能直接当正文（不重复抓取）",
        f"{it.get('fetch_method')} · {it.get('text_chars')} 字")

    # 只有标题、没有正文时：要标出来，但不能当"抓到了"
    it2 = bv.build_item("https://example.invalid/y", None,
                        bv.make_opts(no_model=True), prefetched={"title": "只有标题", "text": ""})
    add(it2.get("kind") in ("unreachable", "too_short") or bool(it2.get("fetch_error")),
        "只有标题时如实标记，不伪装成成功", it2.get("kind", ""))


# ---------------------------------------------------------------------------
# 6. HTTP 接口
# ---------------------------------------------------------------------------
def check_http(py: str) -> None:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))                     # 让系统给个空闲端口，别撞上演示用的 8787
    port = s.getsockname()[1]
    s.close()
    srv = subprocess.Popen(
        [py, str(CODE / "server.py"), "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 别让本机代理插手
    try:
        health = None
        for _ in range(40):
            try:
                health = json.loads(opener.open(base + "/health", timeout=1).read())
                break
            except Exception:                                      # noqa: BLE001
                time.sleep(0.25)
        add(bool(health and health.get("ok")), "服务能起来、/health 正常",
            f"issues={health.get('issues')}" if health else "10 秒内没起来")
        if not health:
            return

        issues = json.loads(opener.open(base + "/issues", timeout=3).read())
        add(issues.get("count") == 16, "/issues 返回 16 个议题", str(issues.get("count")))

        try:
            req = urllib.request.Request(base + "/antipode", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            opener.open(req, timeout=5)
            add(False, "空请求被拒（400）", "竟然返回 200")
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            add(e.code == 400 and body.get("ok") is False, "空请求被拒（400）", body.get("error", "")[:40])
        except Exception as e:                                     # noqa: BLE001
            add(False, "空请求被拒（400）", f"{type(e).__name__}: {e}")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


# ---------------------------------------------------------------------------
# 7. 产物
# ---------------------------------------------------------------------------
ARTIFACTS = [
    ("docs/答辩看板.html", 8_000),
    ("docs/反库演示.html", 8_000),
    ("deck/对跖点路演/对跖点路演.pptx", 20_000),
]


def check_artifacts() -> None:
    stats = [p for p in (ROOT / "data").glob("*.stats.json")]
    newest = max((p.stat().st_mtime for p in stats), default=0)
    for rel, min_size in ARTIFACTS:
        p = ROOT / rel
        if not p.exists():
            add(False, rel, "文件不存在")
            continue
        size = p.stat().st_size
        if size < min_size:
            add(False, rel, f"只有 {size} 字节，像是空的")
            continue
        stale = ("看板" in rel or "演示" in rel) and newest > p.stat().st_mtime + 60
        add(not stale, rel, f"{size // 1024} KB" + ("；比数据旧，重跑生成脚本" if stale else ""))


# ---------------------------------------------------------------------------
# 8. 可选：真跑一条
# ---------------------------------------------------------------------------
LIVE_URL = "https://blog.csdn.net/fudaihb/article/details/136061101"


def check_live(py: str) -> None:
    print(f"  真跑一条：{LIVE_URL}（要网络 + 模型额度，约 1–2 分钟）")
    r = subprocess.run(
        [py, str(CODE / "run_vault.py"), "--vault", str(ROOT / "data/vault_url_only.json"),
         "--limit", "1", "--no-resume", "--quiet",
         "--out", str(ROOT / ".workbuddy/live.jsonl"),
         "--stats", str(ROOT / ".workbuddy/live.stats.json")],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    ok = r.returncode == 0
    note = "见 .workbuddy/live.stats.json"
    if ok:
        try:
            d = json.loads((ROOT / ".workbuddy/live.stats.json").read_text(encoding="utf-8"))
            row = (d.get("rows") or [{}])[0]
            note = f"{row.get('outcome')} · 候选 {row.get('candidates')} · 产出 {row.get('produced')}"
        except Exception:                                          # noqa: BLE001
            pass
    else:
        note = (r.stderr or r.stdout or "")[-120:].replace("\n", " ")
    add(ok, "真跑一条能跑通（网络 + 模型）", note)


def main() -> int:
    ap = argparse.ArgumentParser(description="反仓库全链路自检")
    ap.add_argument("--live", action="store_true", help="额外真跑一条 URL")
    a = ap.parse_args()
    py = sys.executable

    print("=" * 66)
    print("Antipode 反仓库 —— 全链路自检")
    print("=" * 66)

    section("1. 语法");       check_syntax()
    section("2. 依赖");       check_deps()
    section("3. 议题表");     check_taxonomy()
    section("4. 离线链路");   check_engine()
    section("5. 采集层");     check_collect()
    section("6. HTTP 接口");  check_http(py)
    section("7. 产物");       check_artifacts()
    if a.live:
        section("8. 真跑");   check_live(py)

    failed = [n for ok, n, _ in results if ok is False]
    total = len([1 for ok, _, _ in results if ok is not None])
    print("\n" + "=" * 66)
    if failed:
        print(f"❌ {len(failed)}/{total} 项未通过：")
        for n in failed:
            print(f"   · {n}")
        return 1
    print(f"✅ 全部 {total} 项通过" + ("（含真跑）" if a.live else "") + " —— 可以演示了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
