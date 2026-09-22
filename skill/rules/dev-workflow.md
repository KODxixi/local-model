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

公开仓库是受管镜像，**同步 = 单向映射，不要在公开仓库手工改副本**。唯一入口是
[`sync-local-models-public.ps1`](C:/AI/tools/governance/sync-local-models-public.ps1)：

| 公开仓库路径 | 母本来源 |
|---|---|
| `README.md` | 本目录的 `README.md` |
| `skill/AGENTS.md` / `skill/SKILL.md` | 本目录的 `AGENTS.md` / `SKILL.md` |
| `skill/scripts/**` | 本目录的 `scripts/**` |
| `skill/system/**` / `skill/rules/**` / `skill/tools/**` | 本目录的对应目录 |
| `LICENSE`、`.github/workflows/`、根 `AGENTS.md` / `.gitignore` | **仅公开仓库所有**，同步脚本保留 |
| `skill/.gitignore` | 本目录的 `.gitignore` |

发布前以同步脚本内的私有标识预检为准；命中时先修母本，不得绕过检查或直接改公开副本。
