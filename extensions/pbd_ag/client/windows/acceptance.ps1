param(
    [string]$Server = "http://10.11.32.63:8088"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $Root ".venv_windows"
$Wheel = Join-Path $Root "dist\pbd_ag_client-0.1.0-py3-none-any.whl"
$SmokeTest = Join-Path $Root "tests\smoke_test.py"

$Uri = [System.Uri]$Server
$Port = if ($Uri.IsDefaultPort) { 80 } else { $Uri.Port }
$Network = Test-NetConnection -ComputerName $Uri.Host -Port $Port -WarningAction SilentlyContinue
if (-not $Network.TcpTestSucceeded) {
    throw "Cannot reach $($Uri.Host):$Port. Check Wi-Fi, VPN, firewall, and PBD server status."
}

$Created = $false
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.10 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
    if ($LASTEXITCODE -eq 0) {
        & py -3.10 -m venv $Venv
        $Created = $true
    }
}
if ((-not $Created) -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c "import sys; raise SystemExit(sys.version_info < (3, 10))"
    if ($LASTEXITCODE -eq 0) {
        & python -m venv $Venv
        $Created = $true
    }
}
if (-not $Created) {
    throw "Python >=3.10 was not found. Install Python 3.10 or newer from python.org."
}

$Python = Join-Path $Venv "Scripts\python.exe"
$Client = Join-Path $Venv "Scripts\pbd-ag-client.exe"
& $Python -m pip install --no-deps --force-reinstall $Wheel
& $Client --server $Server doctor
& $Python $SmokeTest --server $Server

Write-Host ""
Write-Host "PBD-AG Windows read-only acceptance: PASS" -ForegroundColor Green
Write-Host "Client executable: $Client"
Write-Host "Server: $Server"
Write-Host "No navigation command was issued."
