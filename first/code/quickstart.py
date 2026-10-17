#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令看到成果：用 data/ 里**已经跑出来的真实库**种好平台列表，然后起后端。

    python code/quickstart.py            # 默认 127.0.0.1:8790
    python code/quickstart.py --port 8791
    python code/quickstart.py --reseed   # 覆盖已有 platforms.json（默认不覆盖）

为什么需要它
------------
`data/` 里有真跑出来的库和反库（知乎 10 条 / 16 条反信息，GitHub 5 条），
但 `app_api.py` 只认 `data/platforms.json` 里登记过的平台 —— 没登记就什么都看不到。
这个脚本做的事就是：**扫一遍 data/ 里已有的库，替你把平台登记好**，
于是不用登录、不用联网、不用等十几分钟采集，界面立刻有数据可画。

它只写 `data/platforms.json`（运行时状态，已 gitignore），不动任何库文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIRST = os.path.dirname(_HERE)
_DATA = os.path.join(_FIRST, "data")
_STORE = os.path.join(_DATA, "platforms.json")

try:                                                           # Windows 控制台编码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

# 库是从哪个采集脚本来的 → 对应哪个平台（决定界面上能不能开反推荐）
_BUILDER_PLATFORM = {
    "collect_github.py": "github",
    "collect_zhihu.py": "zhihu",
    "collect_account.py": "url",
    "build_vault.py": "url",
}
_PREFIX_PLATFORM = (("github", "github"), ("zhihu", "zhihu"),
                    ("douyin", "douyin"), ("weibo", "weibo"))


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _platform_of(vault: dict, vid: str) -> str:
    low = vid.lower()
    for prefix, plat in _PREFIX_PLATFORM:
        if low.startswith(prefix):
            return plat
    return _BUILDER_PLATFORM.get(str(vault.get("built_by") or ""), "url")


def scan_vaults() -> list[dict]:
    """扫 data/vault_*.json（不含 .items / .junk），拼出平台记录。"""
    out: list[dict] = []
    if not os.path.isdir(_DATA):
        return out
    for name in sorted(os.listdir(_DATA)):
        if not name.startswith("vault_") or not name.endswith(".json"):
            continue
        path = os.path.join(_DATA, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                vault = json.load(fh)
        except Exception:                                      # noqa: BLE001
            continue
        if not isinstance(vault, dict):
            continue
        vid = str(vault.get("vault_id") or name[len("vault_"):-len(".json")])
        items = _read_jsonl(os.path.join(_DATA, f"vault_{vid}.items.jsonl"))
        if not items:                                          # 没有条目文件，界面上是空的，别登记
            continue
        anti = _read_jsonl(os.path.join(_DATA, f"antipode_{vid}.jsonl"))
        eligible = sum(1 for i in items if i.get("antipode_eligible"))
        plat = _platform_of(vault, vid)
        out.append({
            "id": vid,
            "platform": plat,
            "label": str(vault.get("topic") or vid)[:40],
            "input": str(vault.get("query_str") or ""),
            "limit": len(items),
            "days": int(vault.get("window_days") or 7),
            "lang": "",
            "token": "",
            # 只有库里真的写着开了、且引擎跑出过反信息，才默认开
            "antipode_enabled": bool(vault.get("antipode_enabled")) and bool(anti),
            "status": "done",
            "item_count": len(items),
            "eligible_count": eligible,
            "antipode_count": len(anti),
            "created_at": str(vault.get("created_at") or ""),
            "last_run": str(vault.get("created_at") or ""),
            "last_stage": f"完成：{len(items)} 条信息，{eligible} 条可反推荐，{len(anti)} 条反信息",
            "last_error": "",
            "last_job": "",
        })
    return out


def seed(force: bool) -> int:
    if os.path.exists(_STORE) and not force:
        try:
            with open(_STORE, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            if isinstance(rows, list) and rows:
                print(f"  已存在 data/platforms.json（{len(rows)} 个平台），跳过。要重建加 --reseed")
                return len(rows)
        except Exception:                                      # noqa: BLE001
            pass
    rows = scan_vaults()
    if not rows:
        print("  ⚠️ data/ 里没找到可登记的库（vault_*.json + .items.jsonl）")
        return 0
    os.makedirs(_DATA, exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _STORE)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="用真实产物种好平台列表并起后端")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reseed", action="store_true", help="覆盖已有 platforms.json")
    ap.add_argument("--seed-only", action="store_true", help="只登记平台，不起服务")
    a = ap.parse_args()

    print("=" * 62)
    print("  Antipode · 反仓库 —— 一条命令看到成果")
    print("  时间：" + datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("=" * 62)

    print("\n[1/2] 登记 data/ 里已有的库…")
    n = seed(a.reseed)
    for r in scan_vaults():
        flag = "开" if r["antipode_enabled"] else "关"
        print(f"  · {r['id']:<28} {r['label'][:20]:<22} "
              f"信息 {r['item_count']:>2} / 可反推荐 {r['eligible_count']:>2} / "
              f"反信息 {r['antipode_count']:>2}（反推荐{flag}）")
    if n == 0:
        print("  没东西可看。先跑一次采集，例如：")
        print("    python code/collect_github.py --days 7 --limit 5")
        return 1

    if a.seed_only:
        print("\n完成（--seed-only，未起服务）。")
        return 0

    base = f"http://{a.host}:{a.port}"
    print(f"\n[2/2] 起后端 {base}")
    print("  看数据（另开一个终端，或浏览器直接打开）：")
    print(f"    curl {base}/health")
    print(f"    curl {base}/platforms")
    for r in scan_vaults():
        print(f"    curl \"{base}/platforms/{r['id']}/items?only=eligible\"")
    print("  Ctrl-C 退出\n")

    sys.argv = [os.path.join(_HERE, "app_api.py"), "--host", a.host, "--port", str(a.port)]
    sys.path.insert(0, _HERE)
    import app_api                                              # noqa: PLC0415
    return app_api.main()


if __name__ == "__main__":
    sys.exit(main())
