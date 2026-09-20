#!/usr/bin/env python3
"""Muse Glimmer 30B 的受管入口（2026-09-20 改版：由「自己拉起进程」改为「操作 llama-swap」）。

为什么改：30B 原先由本脚本在 8080 手工拉起、没有 TTL，起了一直待到手动停，
「按需调用」落不了地。现在它和 4 个检索模型一样由 llama-swap(9123) 托管：
请求触发加载、空闲 ttl 自动卸载。本脚本因此不再启动进程，只做三件事：

  status —— 只读：llama-swap 是否在线、30B 是否在场、显存
  start  —— 预热：打一个最小请求把 30B 拉起来（对应 SKILL.md「冷加载的代价用预热补」；
            冷加载实测 15.5s，批量任务开始前先打一下，别让第一条正式请求去付这个时间）
  stop   —— 卸载：调 llama-swap 的 unload 接口立刻还显存（不想等 ttl 到期时用）

注意：30B 与检索模型由 llama-swap 的互斥组保证不同时驻留
（SKILL.md 铁律「检索模型 > 30B」）。所以任何 embedding/rerank 请求都会把 30B 挤下去 ——
`status` 看到「已被挤掉」是正常的，不是故障。

用法：
  <venv python> scripts/muse.py status
  <venv python> scripts/muse.py start
  <venv python> scripts/muse.py stop

端口/模型 id 从 registry.local.yaml 的 muse_glimmer 段读取（本机私有配置，不进 git）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
REGISTRY_LOCAL = SKILL_ROOT / "registry.local.yaml"

DEFAULTS = {
    "base_url": "http://127.0.0.1:9123",
    "model_id": "muse-glimmer-30b",
    "warmup_timeout": 300,
}


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    if REGISTRY_LOCAL.is_file():
        import yaml  # 依赖已在 requirements.txt

        data = yaml.safe_load(REGISTRY_LOCAL.read_text(encoding="utf-8")) or {}
        cfg.update(data.get("muse_glimmer") or {})
    return cfg


def http(url: str, payload: dict | None = None, timeout: float = 10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def running_models(base_url: str) -> list[dict]:
    code, body = http(f"{base_url}/running", timeout=8)
    if code != 200:
        return []
    try:
        return json.loads(body).get("running") or []
    except Exception:
        return []


def vram() -> tuple[int, int] | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip().splitlines()[0]
        used, total = [int(x.strip()) for x in out.split(",")[:2]]
        return used, total
    except Exception:
        return None


def cmd_status(cfg: dict) -> int:
    base, mid = cfg["base_url"], cfg["model_id"]
    code, _ = http(f"{base}/v1/models", timeout=6)
    print(f"Muse Glimmer 30B 受管入口 — llama-swap {base}")
    print(f"  llama-swap: {'在线' if code == 200 else '无响应'}")
    models = running_models(base)
    ids = [m.get("model") for m in models]
    here = mid in ids
    print(f"  30B 在场: {'是' if here else '否'}")
    if here:
        m = next(m for m in models if m.get("model") == mid)
        print(f"    ttl={m.get('ttl')}s  state={m.get('state')}  端口={m.get('proxy')}")
    others = [i for i in ids if i != mid]
    print(f"  检索模型在场: {', '.join(others) if others else '无'}")
    if here and others:
        print("  ⚠️ 30B 与检索模型同时在场 —— 不该发生（llama-swap 互斥组应已阻止），请查 groups 配置")
    mem = vram()
    if mem:
        print(f"  显存: {mem[0]} / {mem[1]} MiB ({round(mem[0] * 100 / mem[1])}%)")
    return 0


def cmd_start(cfg: dict) -> int:
    base, mid = cfg["base_url"], cfg["model_id"]
    if any(m.get("model") == mid for m in running_models(base)):
        print(f"已加载（{mid}），无需预热")
        return 0
    print(f"预热中：向 {base} 发一个最小请求触发加载（冷加载约 15.5s）…")
    t0 = time.time()
    code, _ = http(
        f"{base}/v1/chat/completions",
        {"model": mid, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
        timeout=float(cfg["warmup_timeout"]),
    )
    if code != 200:
        print(f"预热失败：HTTP {code}。查 {base}/running 与 /logs/stream/upstream")
        return 1
    print(f"已就绪：{time.time() - t0:.1f}s（含冷加载）")
    print(f"  注意：空闲 {cfg.get('ttl_hint', 600)}s 后会被 llama-swap 自动卸载；\n"
          f"  期间任何检索请求也会把它挤下去（互斥组，见 SKILL.md「显存与优先级」）。")
    return 0


def cmd_stop(cfg: dict) -> int:
    base, mid = cfg["base_url"], cfg["model_id"]
    if not any(m.get("model") == mid for m in running_models(base)):
        print(f"{mid} 当前不在场（多半是空闲超 ttl 已自动卸载，或被检索请求挤下去了），无需操作")
        return 0
    before = vram()
    code, _ = http(f"{base}/api/models/unload/{mid}", payload={}, timeout=60)
    if code != 200:
        print(f"卸载失败：HTTP {code}（llama-swap 是否在线？）")
        return 1
    after = vram()
    print(f"已卸载 {mid}")
    if before and after:
        print(f"  显存 {before[0]} → {after[0]} MiB（释放 {(before[0] - after[0]) / 1024:.1f} GB）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Muse Glimmer 30B 受管入口（llama-swap 托管）")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="只读：llama-swap / 30B 是否在场 / 显存")
    sub.add_parser("start", help="预热：触发加载 30B")
    sub.add_parser("stop", help="卸载 30B，立刻还显存")
    args = ap.parse_args()
    cfg = load_cfg()
    return {"status": cmd_status, "start": cmd_start, "stop": cmd_stop}[args.action](cfg)


if __name__ == "__main__":
    sys.exit(main())
