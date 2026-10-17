#!/usr/bin/env python3
"""试一试 · 向导版：把自己的账号主页读成一个库。

为什么还要一个向导
----------------
`collect_account.py` 是命令行版：你得先知道自己的主页链接（抖音那种
`/user/MS4wLjABAAAA...` 根本没人记得住），还要会拼一堆参数。
`try_me.py` 把这两件事都省了：

  1. 起一个独立浏览器（登录态固定存在 `first/.profiles/<平台>/`）
  2. **你在里面登录，走到自己主页**（验证码、扫码、短信都由你自己完成）
  3. 程序轮询地址栏，一变成"个人主页"就自动开始读

也就是说：**链接不用你找，程序从地址栏读。**

和 collect_account 的关系
------------------------
同一件事的两种入口。抽链接、建条目、写库用的是同一批函数
（`_EXTRACT_JS` / `build_item` / `write_vault`），产出格式完全一致，
下游 `run_vault.py` 不区分这个库是哪个入口建的。

用法
----
    # 双击 first/试一试.bat，或者在终端里：
    python first/code/try_me.py

    # 已经有主页链接，跳过等待：
    python first/code/try_me.py --platform douyin \\
        --url https://www.douyin.com/user/MS4wLjABAAAA... --limit 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIRST = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 双击运行时窗口一闪就可能关掉，屏幕上的字来不及看。所以每一步都同时写进
# 这个日志：出问题时不用重现，直接读文件就知道走到哪、卡在哪。
# 放在 first/ 下、且被 .gitignore 挡住 —— 里面只有路径和报错，不该进仓库。
_LOG_PATH = os.path.join(_FIRST, ".try_me.log")


def _log(msg: str) -> None:
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:                                          # noqa: BLE001
        pass


def _title(text: str) -> None:
    """改命令行窗口的标题。

    为什么值得做：浏览器一弹出来就把 cmd 窗口盖住了，用户看不到"我在等你
    登录"那几行字，只能靠猜。标题栏在任务栏上一直可见，改成当前阶段，
    不用切窗口也知道程序在干什么。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("对跖点 · " + text)
    except Exception:                                          # noqa: BLE001
        pass

# 控制台编码在 collect_account 被导入时就钉好了（_setup_console：代码页 +
# 输出编码一起设），所以这里不用再管，中文在 cmd / PowerShell / bash 里都正常。

from build_vault import _now, build_item, make_opts, write_vault  # noqa: E402
from collect_account import (  # noqa: E402
    CONTENT_SELECTOR, NAV_LIKE, _EXTRACT_JS, _backend, _block_reason,
    _cookie_env, _dedupe_items, _first_line,
)

# ---------------------------------------------------------------------------
# 1. 支持哪些平台 / 每个平台"走到主页"是什么意思
# ---------------------------------------------------------------------------
PLATFORMS = {
    "douyin": {
        "label": "抖音",
        "start": "https://www.douyin.com",
        # 地址栏长这样才算"到主页了"
        "profile_path": r"^/user/",
        "where": "点右上角你的头像 → 进「我的主页」",
        "note": "想采「喜欢 / 收藏」，进主页后先点那个标签，别急着回车",
    },
    "weibo": {
        "label": "微博",
        "start": "https://weibo.com",
        "profile_path": r"^/(u|n|profile)/",
        "where": "点右上角头像 → 进你的主页",
        "note": "微博主页地址形如 weibo.com/u/1234567890",
    },
    "bilibili": {
        "label": "B站",
        "start": "https://www.bilibili.com",
        "profile_path": r"^/\d+",
        "where": "点右上角头像 → 进「个人空间」",
        "note": "个人空间地址形如 space.bilibili.com/123456",
    },
    "zhihu": {
        "label": "知乎",
        "start": "https://www.zhihu.com",
        "profile_path": r"^/people/",
        "where": "点右上角头像 → 进「我的主页」",
        "note": "主页地址形如 zhihu.com/people/xxx",
    },
    "xiaohongshu": {
        "label": "小红书",
        "start": "https://www.xiaohongshu.com",
        "profile_path": r"^/user/profile/",
        "where": "点左侧「我」→ 进你的主页",
        "note": "主页地址形如 xiaohongshu.com/user/profile/xxx",
    },
}

ORDER = ["douyin", "weibo", "bilibili", "zhihu", "xiaohongshu"]


# ---------------------------------------------------------------------------
# 2. 选平台
# ---------------------------------------------------------------------------
def pick_platform() -> str:
    if not sys.stdin.isatty():
        print("[x] 需要你选一个平台，但当前不是可交互终端。", file=sys.stderr)
        print("    双击 first/试一试.bat，或在终端里跑；也可以加 --platform douyin",
              file=sys.stderr)
        return ""

    print()
    print("  ┌────────────────────────────────────────────────────────┐")
    print("  │  对跖点 · 试一试                                        │")
    print("  │  把你的账号主页读成一个库（只读，不改你任何东西）       │")
    print("  └────────────────────────────────────────────────────────┘")
    print()
    for i, k in enumerate(ORDER, 1):
        print(f"   {i}. {PLATFORMS[k]['label']}")
    print()
    while True:
        try:
            ans = input("  选一个（直接回车＝1 抖音）> ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print()
            if not sys.stdin.isatty():
                print("  这里不是可交互终端，没法问你选哪个。")
                print("  双击 first/试一试.bat 双击运行，或者直接给参数：")
                print("      python first/code/try_me.py --platform douyin")
            else:
                print("  （没收到选择，先退出。）")
            return ""
        if ans.isdigit() and 1 <= int(ans) <= len(ORDER):
            return ORDER[int(ans) - 1]
        if ans in PLATFORMS:
            return ans
        print("  没看懂，输数字就行。")


# ---------------------------------------------------------------------------
# 3. 打开浏览器 → 等你自己走到主页
# ---------------------------------------------------------------------------
# Chromium 错误页的特征。出现这些就该当场说"打不开"，而不是干等到超时 ——
# 用户看到的"bat 打开了但没打开抖音"，一半是这种。
NET_HINTS = (
    ("ERR_NAME_NOT_RESOLVED", "域名解析失败（DNS / 没网）"),
    ("ERR_INTERNET_DISCONNECTED", "本机没联网"),
    ("ERR_PROXY", "代理设置有问题"),
    ("ERR_TUNNEL", "代理连不上目标站"),
    ("ERR_CONNECTION_TIMED_OUT", "连接超时"),
    ("ERR_CONNECTION_RESET", "连接被重置"),
    ("ERR_CONNECTION_CLOSED", "连接被关闭"),
    ("ERR_CONNECTION_REFUSED", "连接被拒绝"),
    ("ERR_ADDRESS_UNREACHABLE", "地址不可达"),
    ("ERR_SSL", "SSL 握手失败（多半是代理劫持）"),
    ("无法访问此网站", "网络层没打通"),
    ("拒绝了我们的连接请求", "连接被拒绝"),
)


def _net_error(text: str) -> str:
    """是不是 Chromium 的「打不开」错误页。是就返回原因，否则空串。"""
    low = text or ""
    for hint, why in NET_HINTS:
        if hint.lower() in low.lower():
            return why
    return ""


def open_and_wait(b, platform: str, a) -> str:
    info = PLATFORMS[platform]
    _title(f"请在浏览器里登录{info['label']}并进主页")
    print(f"\n  正在打开 {info['start']} …", flush=True)
    _log(f"goto {info['start']}")
    try:
        b.goto(info["start"], wait=2.0)
        try:
            cur = b.eval_js("location.href") or "(空)"
        except Exception:                                      # noqa: BLE001
            cur = "(读不到)"
        _log(f"首页已加载，当前 location.href = {cur}")
    except Exception as e:                                     # noqa: BLE001
        _log(f"goto 失败：{type(e).__name__}: {e}")
        print(f"  [!] 打开首页失败：{e}（不影响，你可以在窗口里自己输入网址）")

    print()
    print("  " + "─" * 58)
    print("  接下来在这个新弹出的窗口里做三件事：")
    print(f"     1) 登录你的{info['label']}账号 —— 扫码 / 手机号都行，")
    print("        验证码、短信、滑块全部由你自己过，程序不碰密码")
    print(f"     2) {info['where']}")
    print("     3) 停在那一页别动，程序会自己看到")
    print()
    print(f"  小提示：{info['note']}")
    print("  " + "─" * 58)

    pat = re.compile(info["profile_path"])
    t0 = time.time()
    tick = t0
    net_tick = 0.0
    while time.time() - t0 < a.wait:
        try:
            href = b.eval_js("location.href") or ""
        except Exception:                                      # noqa: BLE001
            href = ""
        path = urlparse(href).path or "/"
        if href.startswith("http") and pat.search(path):
            print(f"\n  [√] 到主页了：{href[:76]}")
            _log(f"到主页：{href}")
            _title("已找到主页，正在读")
            return href

        # 先排掉"根本打不开"：网络断了 / 代理挡了 / DNS 挂了。
        # 这种情况等再久也没用，必须当场说清，别让人以为程序卡死。
        if time.time() - net_tick > 4:
            net_tick = time.time()
            try:
                head = b.eval_js(
                    "(document.body&&document.body.innerText||'').slice(0,600)") or ""
            except Exception:                                  # noqa: BLE001
                head = ""
            bad = _net_error(head)
            if bad:
                print(f"\n  [x] 页面打不开：{bad}")
                print(f"      当前地址：{href[:70] or '(空)'}")
                print("      这是网络层的问题，不是程序的问题。先用普通浏览器打开同一个")
                print("      网址确认一下；那边也转圈的话，就是网络 / 代理的事。")
                return ""

        # 每 12 秒报一次，免得让人以为卡死
        if time.time() - tick > 12:
            tick = time.time()
            left = int(a.wait - (time.time() - t0))
            print(f"  … 还在等（剩 {left}s）  当前页面：{path[:44] or '(空白)'}")
            try:
                head = b.eval_js(
                    "(document.body&&document.body.innerText||'').slice(0,200)") or ""
            except Exception:                                  # noqa: BLE001
                head = ""
            why = _block_reason(head + " " + path)
            if why:
                print(f"  [!] 页面像是被拦了：{why} —— 在窗口里手动过一下就行，"
                      "程序会继续等")
        time.sleep(1.5)

    print(f"\n  [x] 等了 {int(a.wait)} 秒还没看到主页。")
    print("      可能原因：没登录成功 / 停在首页没进主页 / 网址不对。")
    print("      想再来一次，直接重新运行；也可以把主页链接贴给我，用 --url 给。")
    return ""


# ---------------------------------------------------------------------------
# 4. 滚动 + 抽链接（和 collect_account 同一套逻辑）
# ---------------------------------------------------------------------------
def extract_rows(b, platform: str, a) -> list[dict]:
    if a.scroll:
        h = b.scroll(a.scroll, a.scroll_pause)
        print(f"  滚了 {a.scroll} 次，页面高度 {h}px")

    try:
        title = b.eval_js("document.title") or ""
    except Exception:                                          # noqa: BLE001
        title = ""

    sel = CONTENT_SELECTOR.get(platform, CONTENT_SELECTOR["generic"])
    rows = _run_extract(b, sel, a)

    # 平台改版是常态：专用选择器抽不到就退回通用选择器，别整条链路失效
    if not rows and sel != CONTENT_SELECTOR["generic"]:
        rows = [r for r in _run_extract(b, "a[href]", a)
                if len((r.get("text") or "")) >= 8]
        if rows:
            print("  （专用选择器没抽到，已退回通用选择器 —— 平台可能改版了）")

    print(f"  页面「{title[:30]}」抽到 {len(rows)} 个链接")

    if not rows:
        try:
            head = b.eval_js(
                "(document.body&&document.body.innerText||'').slice(0,400)") or ""
        except Exception:                                      # noqa: BLE001
            head = ""
        why = _block_reason(title + " " + head)
        if why:
            print(f"  [x] 被平台拦了：{why}")
            print("      在窗口里手动过验证码，然后重新运行一次。")
        else:
            print("  [!] 一个都没抽到。可能是：主页里没有内容 / 没登录 / 平台改版。")
        return []

    return rows


def _run_extract(b, sel: str, a) -> list[dict]:
    js = (_EXTRACT_JS
          .replace("__SEL__", json.dumps(sel))
          .replace("__MAX__", str(max(60, (a.limit or 10) * 4)))
          .replace("__NAVSRC__", json.dumps(NAV_LIKE.pattern)))
    try:
        return b.eval_js(js) or []
    except Exception as e:                                     # noqa: BLE001
        print(f"  [!] 抽链接时出错：{e}")
        return []


def keep_same_site(rows: list[dict], page_url: str, limit: int) -> list[dict]:
    host = urlparse(page_url).hostname or ""
    out: list[dict] = []
    for r in rows:
        h = urlparse(r.get("href", "")).hostname or ""
        if h and h.split(".")[-2:] != host.split(".")[-2:]:
            continue
        text = re.sub(r"\s+", " ", (r.get("text") or "")).strip()
        out.append({
            "url": r["href"],
            "title": _first_line(text),
            "hint_text": text,
        })
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# 5. 逐条建条目 + 写库（与 collect_account 的产出完全一致）
# ---------------------------------------------------------------------------
def build_vault(raw: list[dict], platform: str, page_url: str, a, cookie: str) -> int:
    if cookie:
        os.environ["ANTIPODE_COOKIES"] = _cookie_env(cookie, platform, a)

    opts = make_opts(timeout=a.timeout, max_chars=a.max_chars,
                     min_chars=a.min_chars, no_model=a.no_model)
    backend = None if a.no_model else _backend()

    print()
    items: list[dict] = []
    for n, r in enumerate(raw, 1):
        # 列表页已给标题/片段：抓不到正文时用它兜底，别让整条信息消失
        pre = {"title": r.get("title", ""), "text": r.get("hint_text", "")}
        it = build_item(r["url"], backend, opts, prefetched=pre)
        it["collected_by"] = f"try_me.py:{platform}"
        if r.get("hint_text") and it.get("kind") in ("unreachable", "too_short"):
            it["summary"] = _first_line(r["hint_text"], 90)
            it["summary_from"] = "list_page"
        items.append(it)
        flag = "★" if it.get("antipode_eligible") else " "
        print(f"  {flag} {n:>2}. {(it.get('title') or '(无标题)')[:30]:<32}"
              f" {it.get('kind', ''):<12} {it.get('text_chars', 0):>5}字")

    items = _dedupe_items(items)
    if a.limit:
        items = items[: a.limit]

    eligible = [i for i in items if i.get("antipode_eligible")]
    kinds: dict[str, int] = {}
    for i in items:
        kinds[i.get("kind", "?")] = kinds.get(i.get("kind", "?"), 0) + 1

    vault_id = a.vault_id or f"{platform}-{_now()[5:16].replace(':', '').replace('-', '')}"
    vault = {
        "vault_id": vault_id,
        "topic": a.topic or f"我的{PLATFORMS[platform]['label']}主页",
        "antipode_enabled": a.antipode == "on",
        "created_at": _now(),
        "built_by": "try_me.py",
        "source": f"account:{platform}",
        "account": {
            "platform": platform,
            "url": page_url,
            "scope": "profile",
            "auth": "browser",
        },
        "item_count": len(items),
        "antipode_eligible_count": len(eligible),
        "kinds": kinds,
    }
    vpath, ipath = write_vault(vault, items, a.out_dir)

    print()
    print("  " + "-" * 66)
    print(f"  入库 {len(items)} 条    ★ 可参与反推荐 {len(eligible)} 条")
    print("  类型：" + "  ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))
    print()
    print(f"  → {os.path.relpath(vpath, os.path.dirname(_FIRST))}")
    print(f"  → {os.path.relpath(ipath, os.path.dirname(_FIRST))}")
    print()
    if not eligible:
        print("  说明：这批没有可参与反推荐的条目。反推荐只对「有观点的长文/帖子」生效，")
        print("        短视频、纯转发、作品集这类没有对立面 —— 它们只是被归档了。")
        print()
    print("  下一步：")
    print(f"    python first/code/run_vault.py --vault "
          f"{os.path.relpath(vpath, os.path.dirname(_FIRST))}")
    return 0


# ---------------------------------------------------------------------------
# 6. 主流程
# ---------------------------------------------------------------------------
def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="试一试：把自己的账号主页读成一个库（向导版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python first/code/try_me.py --platform douyin --limit 8",
    )
    ap.add_argument("--platform", default="", choices=[""] + ORDER,
                    help="平台（不给就问你）")
    ap.add_argument("--url", default="",
                    help="主页链接；给了就不等你在浏览器里走过去")
    ap.add_argument("--limit", type=int, default=10, help="最多几条（默认 10）")
    ap.add_argument("--scroll", type=int, default=8, help="往下滚几次触发懒加载")
    ap.add_argument("--scroll-pause", type=float, default=1.2)
    ap.add_argument("--wait", type=float, default=300.0, help="最长等你登录多久（秒）")
    ap.add_argument("--settle", type=float, default=5.0,
                    help="看到主页后再等几秒（你要点「喜欢/收藏」标签就用得上）")
    ap.add_argument("--profile", default="", help="登录态目录（默认 first/.profiles/<平台>）")
    ap.add_argument("--topic", default="")
    ap.add_argument("--vault-id", default="")
    ap.add_argument("--antipode", choices=["on", "off"], default="on")
    ap.add_argument("--no-model", action="store_true",
                    help="不调模型（没配 key 时先用这个）")
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--min-chars", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out-dir", default=os.path.join(_FIRST, "data"))
    ap.add_argument("--keep-open", action="store_true", help="跑完不关浏览器窗口")
    a = ap.parse_args(argv)

    platform = a.platform or pick_platform()
    if not platform:
        return 2

    from browser_cdp import Browser, has_websockets

    if not has_websockets():
        print("[x] 这条路需要一个库：websockets", file=sys.stderr)
        print("    装一次就行：<python> -m pip install websockets", file=sys.stderr)
        return 3

    info = PLATFORMS[platform]
    profile = a.profile or os.path.join(_FIRST, ".profiles", platform)
    os.makedirs(profile, exist_ok=True)

    print()
    print("=" * 70)
    print(f"  试一试   平台 {info['label']}   登录态 {os.path.relpath(profile, os.path.dirname(_FIRST))}")
    print("=" * 70)

    b = Browser(profile_dir=profile, headless=False, keep_open=a.keep_open)
    _log(f"平台={platform} profile={profile} exe={b.exe} port={b.port}")
    _title("正在启动浏览器")
    print(f"\n  [1/3] 正在启动浏览器（3~10 秒，窗口可能先白一下）…", flush=True)
    try:
        b.start()
        _log("浏览器已启动，调试端口通了")
        print(f"  [2/3] 浏览器起来了。地址栏会自动填 {info['start']}", flush=True)
        page_url = a.url or open_and_wait(b, platform, a)
        if not page_url:
            _log("没等到主页（超时或页面打不开）")
            return 4

        if a.settle:
            print(f"\n  {int(a.settle)} 秒后开始读。"
                  "（要采「喜欢 / 收藏」的话，现在点那个标签）")
            for i in range(int(a.settle), 0, -1):
                print(f"    {i} …", end="\r", flush=True)
                time.sleep(1)
            print(" " * 20, end="\r")

        print(f"\n  [3/3] 开始读页面（抽链接 → 抓正文 → 判类型）…", flush=True)
        _log("开始抽取")
        rows = extract_rows(b, platform, a)
        _log(f"抽到 {len(rows or [])} 个候选链接")
        if not rows:
            return 5

        raw = keep_same_site(rows, page_url, a.limit)
        if not raw:
            print("  [!] 抽到的链接都不属于这个站点，可能停在别人的页面上。")
            return 5

        cookie = ""
        try:
            cookie = b.cookie_header(urlparse(page_url).hostname or "")
        except Exception:                                      # noqa: BLE001
            pass
    finally:
        b.close()

    return build_vault(raw, platform, page_url, a, cookie)


def main(argv=None) -> int:
    """外层兜底。

    为什么非要有这一层：双击 bat 时窗口是 cmd 的，程序一崩，traceback 会
    一闪而过然后停在 `pause`，用户只看到"按任意键继续"—— 表现就是
    "按了回车，什么都没发生"。所以这里把任何异常都翻成人话、写到日志，
    并在屏幕上留一句"日志在哪"，让失败本身变成可读的信息。
    """
    try:
        with open(_LOG_PATH, "w", encoding="utf-8") as f:
            f.write(f"=== try_me 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            f.write(f"argv = {argv if argv is not None else sys.argv[1:]}\n")
            f.write(f"python = {sys.executable}\n")
            f.write(f"cwd = {os.getcwd()}\n")
        _title("准备中")
        rc = _main(argv)
        _title("完成，可以关窗" if rc == 0 else "结束了 —— 看上面的提示")
        return rc
    except KeyboardInterrupt:
        print("\n  （按了 Ctrl+C，先退出。）")
        _log("KeyboardInterrupt")
        _title("已中断")
        return 130
    except Exception:                                          # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        _log("崩溃：\n" + tb)
        _title("出错了 —— 看日志")
        print()
        print("  " + "=" * 60)
        print("  [x] 程序出错了。下面是完整原因，直接截这一段发给我就行：")
        print("  " + "=" * 60)
        print()
        print(tb)
        print("  " + "-" * 60)
        print(f"  同样的内容也写进了：{_LOG_PATH}")
        return 1


if __name__ == "__main__":
    import atexit

    from collect_account import _cleanup_temp

    atexit.register(_cleanup_temp)
    sys.exit(main())
