# 开发工作流

> 开发者维护指南

## 首次使用

```powershell
# 在仓库根目录执行（脚本自己定位所在目录，不依赖 cwd）
powershell -ExecutionPolicy Bypass -File <SKILL_ROOT>\setup.ps1
```

安装完成后用 venv Python 运行所有命令：

```powershell
<SKILL_ROOT>\.venv\Scripts\python.exe <SKILL_ROOT>\scripts\cli.py doctor
```

## 开发流程

1. 改代码（Skill 层改 `scripts/`）
2. 跑聚焦检查：
   ```powershell
   <SKILL_ROOT>\.venv\Scripts\python.exe -m pytest tests -m "not integration"
   <SKILL_ROOT>\.venv\Scripts\python.exe -m ruff check scripts tests
   ```
3. `integration` 用例需要 llama-swap / 对话模型在线，离线会自动跳过
4. 提交 PR

## 改动边界

- `registry.yaml` 只放示例配置
- 不要把本机真实路径、私有库名、个人目录写进任何被 git 跟踪的文件
- 所有个人绝对路径写成 `<SKILL_ROOT>` / `<MODELS_DIR>` / `<LLAMA_CPP_DIR>` 占位符
- 真实路径只出现在 `registry.local.yaml`（已 gitignore）

## 文档与发布（维护者）

本仓库是公开镜像，**同步 = 纯拷贝，不要在本仓库手工改副本**：

| 本仓库路径 | 来源 |
|---|---|
| `README.md` / `AGENTS.md` | skill 层目录根的 `README.md` / `AGENTS.md` |
| `scripts/**` | skill 层 scripts 目录 |
| `system/**` / `rules/**` / `tools/**` | skill 层对应目录 |
| `LICENSE`、`.github/workflows/`、根 `.gitignore` | **仅本仓库所有**，母库无对应文件 |

发布前自检：在仓库根执行 `git grep -nE "C:\\\\Users|<你的私有目录名>"` 应为 0 命中。
