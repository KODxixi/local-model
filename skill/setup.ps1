# local-model Skill 一键安装脚本
# 用法: powershell -ExecutionPolicy Bypass -File setup.ps1
# 功能: 创建 venv、安装依赖、验证安装

$ErrorActionPreference = "Stop"
$SkillDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $SkillDir ".venv"
$Requirements = Join-Path $SkillDir "requirements.txt"

Write-Host "=== local-model Skill 安装 ===" -ForegroundColor Cyan
Write-Host "Skill 目录: $SkillDir"

# 1. 检查 Python
Write-Host "`n[1/4] 检查 Python..." -ForegroundColor Yellow
try {
    $PythonCmd = "python"
    $Version = & $PythonCmd --version 2>&1
    Write-Host "  Python: $Version"
} catch {
    Write-Host "  错误: 未找到 python，请先安装 Python 3.11+" -ForegroundColor Red
    exit 1
}

# 2. 创建 venv
Write-Host "`n[2/4] 创建虚拟环境..." -ForegroundColor Yellow
if (-not (Test-Path $VenvDir)) {
    & $PythonCmd -m venv $VenvDir
    Write-Host "  已创建: $VenvDir"
} else {
    Write-Host "  已存在，跳过创建"
}

# 3. 激活 venv 并安装依赖
Write-Host "`n[3/4] 安装依赖..." -ForegroundColor Yellow
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "  错误: venv Python 不存在: $VenvPython" -ForegroundColor Red
    exit 1
}

# 升级 pip
& $VenvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null
Write-Host "  pip 已升级"

# 安装依赖
if (Test-Path $Requirements) {
    & $VenvPython -m pip install -r $Requirements
    Write-Host "  依赖安装完成"
} else {
    Write-Host "  警告: requirements.txt 不存在: $Requirements" -ForegroundColor Yellow
}

# 4. 验证安装
Write-Host "`n[4/4] 验证安装..." -ForegroundColor Yellow
$CliScript = Join-Path $SkillDir "scripts\cli.py"
if (Test-Path $CliScript) {
    Write-Host "  运行 doctor 诊断..."
    & $VenvPython $CliScript doctor
} else {
    Write-Host "  跳过: cli.py 不存在"
}

Write-Host "`n=== 安装完成 ===" -ForegroundColor Green
Write-Host "使用方法:"
Write-Host "  激活 venv: .venv\Scripts\Activate.ps1"
Write-Host "  运行 CLI: python scripts\cli.py doctor"
Write-Host "  或直接: .venv\Scripts\python.exe scripts\cli.py <command>"
