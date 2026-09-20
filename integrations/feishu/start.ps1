$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Requirements = Join-Path $PSScriptRoot "requirements.txt"
$EntryPoint = Join-Path $PSScriptRoot "run.py"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "首次启动：正在创建项目运行环境..."
    py -3.10 -m venv (Join-Path $ProjectRoot ".venv")
}

& $VenvPython -c "import lark_channel, dotenv, requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "正在安装飞书桥接依赖..."
    & $VenvPython -m pip install -r $Requirements
}

Set-Location -LiteralPath $ProjectRoot
& $VenvPython $EntryPoint
exit $LASTEXITCODE
