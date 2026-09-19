#!/usr/bin/env python3
"""Muse Glimmer 30B（8080）的受管启停入口。

为什么要有它：30B 没有 TTL、不是计划任务，过去只能手工敲一整条 llama-server 命令，
于是"按需调用"落不了地、也没人知道它什么时候该停。本脚本把启停变成一个动作。

规则（见 SKILL.md「显存与优先级」）：
  - 一切本地模型按需调用，不常驻
  - 优先级：向量/检索模型 > 30B
  - 30B 只在打标 / 批量视觉时按需起，用完停
  - 显存不足时**拒绝启动**并说明谁在占，不要自行硬起（除非显式 --yes）

私有路径从 registry.local.yaml 的 muse_glimmer 段读取（本机配置，不进 git）。

用法：
  <venv python> scripts/muse.py status
  <venv python> scripts/muse.py start [--yes]
  <venv python> scripts/muse.py stop
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
REGISTRY_LOCAL = SKILL_ROOT / "registry.local.yaml"


def load_cfg() -> dict:
    if not REGISTRY_LOCAL.is_file():
        sys.exit(f"缺少 {REGISTRY_LOCAL}（本机私有配置，含 30B 的路径）")
    import yaml  # 依赖已在 requirements.txt

    data = yaml.safe_load(REGISTRY_LOCAL.read_text(encoding="utf-8")) or {}
    cfg = data.get("muse_glimmer")
    if not cfg:
        sys.exit(
            "registry.local.yaml 里没有 muse_glimmer 段。示例：\n"
            "muse_glimmer:\n"
            "  llama_server: <LLAMA_CPP_DIR>\\llama-server.exe\n"
            "  models_dir: <MODELS_DIR>\\Muse\n"
            "  model: Muse-Glimmer-30B-....gguf\n"
            "  mmproj: mmproj-....gguf\n"
            "  draft_model: dflash-....gguf\n"
            "  port: 8080\n"
            "  context: 32768\n"
            "  threads: 20\n"
            "  parallel: 1\n"
            "  min_free_gb: 24\n"
            "  log: <临时目录>\\muse-glimmer.log"
        )
    return cfg


def http_get(url: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def port_serving(port: int) -> bool:
    code, _ = http_get(f"http://127.0.0.1:{port}/v1/models")
    return code == 200


def ps(command: str) -> str:
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True, errors="replace",
    )
    return (out.stdout or "").strip()


def find_pid(port: int) -> int | None:
    """按命令行里的 --port <port> 找 llama-server 进程（比按端口号更准，避免误杀）。"""
    cmd = (
        "$p = Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '--port {port}(\\s|$)' }} | "
        "Select-Object -First 1; if ($p) { $p.ProcessId }"
    )
    out = ps(cmd)
    return int(out) if out.isdigit() else None


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


def llama_swap_running() -> list[str]:
    code, body = http_get("http://127.0.0.1:9123/running")
    if code != 200:
        return []
    try:
        return [m.get("model", "?") for m in json.loads(body).get("running", [])]
    except Exception:
        return []


def cmd_status(cfg: dict) -> int:
    port = int(cfg["port"])
    pid = find_pid(port)
    print(f"30B (Muse Glimmer) 受管入口 — 端口 {port}")
    print(f"  进程: {'PID ' + str(pid) if pid else '未运行'}")
    print(f"  端口: {'在服务' if port_serving(port) else '无响应'}")
    mem = vram()
    if mem:
        used, total = mem
        print(f"  显存: {used} / {total} MiB ({round(used * 100 / total)}%)")
    rl = llama_swap_running()
    print(f"  检索模型(llama-swap 9123)已加载: {', '.join(rl) if rl else '无'}")
    if pid and rl:
        print("  ⚠️ 30B 与检索模型同时在场 —— 按规则应让 30B 让路（优先级：检索 > 30B）")
    return 0


def cmd_start(cfg: dict, yes: bool) -> int:
    port = int(cfg["port"])
    if port_serving(port):
        print(f"已在运行（PID {find_pid(port)}），不重复启动")
        return 0
    rl = llama_swap_running()
    mem = vram()
    need = float(cfg.get("min_free_gb", 24))
    if mem and rl:
        free = (mem[1] - mem[0]) / 1024
        if free < need and not yes:
            print(f"拒绝启动：空闲显存 {free:.1f} GB < 需要的 {need} GB，且检索模型在场：")
            print(f"  {', '.join(rl)}")
            print("  按规则（SKILL.md「显存与优先级」）不要自行硬起：")
            print("  要么等检索模型 TTL 卸载（空闲 5 分钟），要么请用户决定启停顺序。")
            print("  确认要起就加 --yes。")
            return 2
    models = Path(cfg["models_dir"])
    cmd = [
        str(cfg["llama_server"]),
        "--gpu-layers", "99",
        "-m", str(models / cfg["model"]),
        "--mmproj", str(models / cfg["mmproj"]),
        "--spec-type", "draft-dflash",
        "--spec-draft-model", str(models / cfg["draft_model"]),
        "--spec-draft-n-max", str(cfg.get("draft_n_max", 10)),
        "-fa", "on",
        "-c", str(cfg.get("context", 32768)),
        "--threads", str(cfg.get("threads", 20)),
        "--parallel", str(cfg.get("parallel", 1)),
        "--port", str(port),
        "--host", "127.0.0.1",
    ]
    for p in (Path(cmd[0]), Path(cmd[3]), Path(cmd[5]), Path(cmd[7])):
        if not p.is_file():
            print(f"缺少文件: {p}")
            return 1
    log_path = cfg.get("log") or str(Path(os.environ.get("TEMP", ".")) / "muse-glimmer.log")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        subprocess.Popen(
            cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    print(f"已启动，等待就绪（日志 {log_path}）…")
    t0 = time.time()
    while time.time() - t0 < 300:
        if port_serving(port):
            print(f"就绪：{time.time() - t0:.1f}s（冷加载实测）")
            return 0
        time.sleep(3)
    print("300 秒内未就绪，请看日志")
    return 1


def cmd_stop(cfg: dict) -> int:
    port = int(cfg["port"])
    pid = find_pid(port)
    if not pid:
        print("未发现运行中的 30B（按 --port 匹配）")
        return 0
    before = vram()
    ps(f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")
    for _ in range(20):
        if not port_serving(port):
            break
        time.sleep(1)
    after = vram()
    print(f"已停止 PID {pid}")
    if before and after:
        print(f"  显存 {before[0]} → {after[0]} MiB（释放 {(before[0] - after[0]) / 1024:.1f} GB）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Muse Glimmer 30B 受管启停")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="只读：进程/端口/显存/检索模型是否在场")
    p_start = sub.add_parser("start", help="启动 30B（显存不足时拒绝，除非 --yes）")
    p_start.add_argument("--yes", action="store_true", help="显存不足也强行启动")
    sub.add_parser("stop", help="停止 30B 并释放显存")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.action == "status":
        return cmd_status(cfg)
    if args.action == "start":
        return cmd_start(cfg, args.yes)
    return cmd_stop(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
