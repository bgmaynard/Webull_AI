# WeBull_AI Trading Bot - Graceful shutdown
# Kills backend and frontend processes

$logDir = "C:\WeBull_AI\logs"
$date = Get-Date -Format "yyyy-MM-dd"
$logFile = "$logDir\bot_$date.log"

Write-Output "$(Get-Date -Format 'HH:mm:ss') Shutting down trading bot..." | Tee-Object -FilePath $logFile -Append

# Kill backend (port 9100)
$procs = Get-NetTCPConnection -LocalPort 9100 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($pid in $procs) {
    Write-Output "$(Get-Date -Format 'HH:mm:ss') Stopping backend (PID $pid)" | Tee-Object -FilePath $logFile -Append
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
}

# Kill frontend (port 5173)
$procs = Get-NetTCPConnection -LocalPort 5173 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($pid in $procs) {
    Write-Output "$(Get-Date -Format 'HH:mm:ss') Stopping frontend (PID $pid)" | Tee-Object -FilePath $logFile -Append
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
}

Write-Output "$(Get-Date -Format 'HH:mm:ss') Bot stopped." | Tee-Object -FilePath $logFile -Append
