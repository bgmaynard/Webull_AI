# WeBull_AI Trading Bot - Auto-start script
# Scheduled to run at 4:00 AM ET for premarket discovery
#
# What happens on launch:
#   1. Backend starts (webull_trading_api.py on port 9300)
#   2. Scanner pipeline auto-starts → scans premarket gainers every 60s
#   3. Scalper auto-enables and starts → monitors momentum + executes trades
#   4. Frontend dev server starts (port 5173) for dashboard access
#
# Logs written to: C:\WeBull_AI\logs\bot_YYYY-MM-DD.log

$ErrorActionPreference = "Continue"
$botDir = "C:\WeBull_AI"
$logDir = "$botDir\logs"
$date = Get-Date -Format "yyyy-MM-dd"
$logFile = "$logDir\bot_$date.log"

# Create log directory
if (!(Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# Load environment variables from .env
Get-Content "$botDir\.env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -and !$line.StartsWith("#") -and $line.Contains("=")) {
        $parts = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
    }
}

# Kill any existing bot processes on our ports
$procs = Get-NetTCPConnection -LocalPort 9300 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($pid in $procs) {
    Write-Output "$(Get-Date -Format 'HH:mm:ss') Killing existing process on port 9300 (PID $pid)" | Tee-Object -FilePath $logFile -Append
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
}

# Start backend
Write-Output "$(Get-Date -Format 'HH:mm:ss') Starting trading bot backend..." | Tee-Object -FilePath $logFile -Append
$backend = Start-Process -FilePath "python" `
    -ArgumentList "webull_trading_api.py" `
    -WorkingDirectory $botDir `
    -RedirectStandardOutput "$logDir\backend_$date.log" `
    -RedirectStandardError "$logDir\backend_err_$date.log" `
    -PassThru -WindowStyle Hidden

Write-Output "$(Get-Date -Format 'HH:mm:ss') Backend started (PID $($backend.Id))" | Tee-Object -FilePath $logFile -Append

# Wait for backend to be ready
Start-Sleep -Seconds 5

# Start frontend dev server
Write-Output "$(Get-Date -Format 'HH:mm:ss') Starting dashboard frontend..." | Tee-Object -FilePath $logFile -Append
$frontend = Start-Process -FilePath "npm" `
    -ArgumentList "run", "dev" `
    -WorkingDirectory "$botDir\ui\trading" `
    -RedirectStandardOutput "$logDir\frontend_$date.log" `
    -RedirectStandardError "$logDir\frontend_err_$date.log" `
    -PassThru -WindowStyle Hidden

Write-Output "$(Get-Date -Format 'HH:mm:ss') Frontend started (PID $($frontend.Id))" | Tee-Object -FilePath $logFile -Append
Write-Output "$(Get-Date -Format 'HH:mm:ss') Bot ready — Dashboard: http://localhost:5173 | API: http://localhost:9300" | Tee-Object -FilePath $logFile -Append
