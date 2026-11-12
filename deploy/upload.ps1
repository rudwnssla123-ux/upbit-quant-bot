# Upload bot files from this Windows PC to the Ubuntu server and run setup_server.sh there.
# Usage (from the project folder):
#   powershell -ExecutionPolicy Bypass -File deploy\upload.ps1 -ServerHost 1.2.3.4 -KeyFile C:\path\to\key.pem
# Safe to re-run for code updates: the server-side state file (bot_v5_state.json) is never overwritten.
param(
    [Parameter(Mandatory = $true)] [string]$ServerHost,
    [Parameter(Mandatory = $true)] [string]$KeyFile,
    [string]$User = "ubuntu"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$files = @("bot_v5.py", "strategy_v5.py", "upbit_market.py", "backtest_v5.py", "backup_state.py", "monthly_check.py", "add_capital.py", "requirements.txt", "bot_v5.service", "deploy\setup_server.sh", ".env")
foreach ($f in $files) {
    if (-not (Test-Path $f)) { throw "Missing file: $f  (copy .env.example to .env and fill it in first)" }
}
if (-not (Test-Path $KeyFile)) { throw "Key file not found: $KeyFile" }

# Windows OpenSSH refuses private keys readable by other users -> restrict to the current user.
icacls $KeyFile /inheritance:r /grant:r "$($env:USERNAME):R" | Out-Null

$target = "$User@$ServerHost"
Write-Host "1/3 Creating ~/bot on $target"
ssh -i $KeyFile -o StrictHostKeyChecking=accept-new $target "mkdir -p ~/bot"
if ($LASTEXITCODE -ne 0) { throw "SSH connection failed (check IP, key file, firewall port 22)" }

Write-Host "2/3 Uploading files"
scp -i $KeyFile $files "${target}:~/bot/"
if ($LASTEXITCODE -ne 0) { throw "Upload failed" }

Write-Host "3/3 Running setup on server"
ssh -t -i $KeyFile $target "bash ~/bot/setup_server.sh"
if ($LASTEXITCODE -ne 0) { throw "Setup reported an error (see messages above)" }
