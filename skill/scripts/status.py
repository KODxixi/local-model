#!/usr/bin/env python3
r"""status.py — 本机本地模型栈的**只读**状态查询（调用前预检的唯一入口）。

2026-09-21 甲-1 后：**单一入口 9123**（llama-swap 管全部 4 个模型）。8080 已停用。

设计约束（来自独立审计）：
  · **只发 GET**。绝不 POST，绝不触发任何模型加载 —— 否则"查状态"本身就成了写操作。
    （反例：`cli.py doctor` 会发一次真实 embedding 推理）
  · **一处回答**"现在能调什么、什么被占着、加载 X 会踹掉谁"。
  · 不复制任何显存数字：现读现算。

退出码：0 = 正常；2 = 服务不可达
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

LAP = "http://127.0.0.1:9123"
# 会触发 exclusive 驱逐的（读自 config.yaml 的 groups）
EMBED_GROUPS = {
    "text-embedding-qwen3-embedding-8b": "vl-embedding-2b",
    "vl-embedding-2b": "text-embedding-qwen3-embedding-8b",
}
PERSISTENT = {"vl-reranker-2b", "muse-glimmer-30b"}
COLD = {
    "text-embedding-qwen3-embedding-8b": "4.29s",
    "vl-embedding-2b": "12.31s",
    "vl-reranker-2b": "16.27s",
    "muse-glimmer-30b": "~54s",
}


def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code}
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


def gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        u, f = [int(x.strip()) for x in out.split(",")]
        return u, f
    except Exception:
        return None, None


def main() -> int:
    print("=" * 70)
    print("本机本地模型栈 · 只读预检（GET only，不触发任何加载）")
    print("单一入口 9123 —— llama-swap 管全部 4 个模型（甲-1，2026-09-21）")
    print("=" * 70)

    print("\n【模型清单】")
    models = get(f"{LAP}/v1/models")
    if "__error__" in models or "__http_error__" in models:
        print(f"  ✗ 9123 不可达：{models.get('__error__') or models.get('__http_error__')}")
        print("    → 全部本地模型调用都会失败")
        print("    → 查：schtasks /Query /TN LlamaSwap   （启动：schtasks /Run /TN LlamaSwap）")
        print("    → 不要手工 kill 检索进程；不要指望 8080（已停用）")
        return 2

    loaded = []
    for m in models.get("data", []):
        st = (m.get("status") or {}).get("value", "?")
        mark = {"loaded": "●", "unloaded": "○"}.get(st, "?")
        tag = "  ← 常驻" if m["id"] in PERSISTENT and st == "loaded" else ""
        print(f"  {mark} {m['id']:38s} {st}{tag}")
        if st == "loaded":
            loaded.append(m["id"])

    running = get(f"{LAP}/running") or {}
    ttls = {m.get("model"): m.get("ttl") for m in (running.get("running") or [])}

    print("\n【在途】")
    if "muse-glimmer-30b" in ttls:
        print(f"  muse-glimmer-30b: 已加载，ttl={ttls.get('muse-glimmer-30b')}s"
              f"（空闲到期自动卸载 —— 这就是「自主卸载」）")
    else:
        print("  muse-glimmer-30b: 未加载 —— 下一个请求会触发冷加载（~54s）")
    print("  ⚠️ 该模型 `--parallel 1` 单槽：**客户端并发必须 = 1**，多发只会排队，")
    print("     排队时间叠加到客户端超时上（archlib 打标 timeout=90s）")

    used, free = gpu()
    print("\n【显存（nvidia-smi 现读）】")
    if used is None:
        print("  ? nvidia-smi 不可用")
    else:
        print(f"  整卡 {used + free} MiB · 已用 {used} · **余 {free}**")
        if free < 700:
            print("  ⚠️ 余 < 700 MiB：额外的模型装卸有 OOM 风险。")
            print("     余量几乎完全由**桌面侧**决定 —— 先关吃 GPU 的程序（浏览器/Overlay/ToDesk），")
            print("     而不是去调模型参数。")

    print("\n【加载 X 会踹掉谁（读自 config.yaml 的 groups）】")
    for want, victim in EMBED_GROUPS.items():
        print(f"    加载 {want:38s} → 会驱逐 {victim}")
    print("    （vl-reranker-2b 与 muse-glimmer-30b 是 persistent，不会被驱逐）")
    print("  ⚠️ 被驱逐后再调用要付冷加载（见下表）—— OpenClaw memory_search 有**不可配的 30s 硬上限**。")
    print("     会触发的事：`--kb <图文库>` / `search-image` / `verify-local-models.ps1 -Live`。")
    print("     （`--kb all` 已默认跳过 multimodal 库。）")

    print("\n【冷加载代价】")
    for k in ("muse-glimmer-30b", "vl-embedding-2b", "vl-reranker-2b", "text-embedding-qwen3-embedding-8b"):
        print(f"    {k:38s} {COLD.get(k, '?')}")

    print("\n" + "=" * 70)
    print("结论：9123 可用。调用前对照上面「已加载」列表 —— 不在列表里的要等冷加载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
