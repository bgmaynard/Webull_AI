# Webull Trading Bot - Startup Script
# Run from project root: .\startup\start_bot.ps1

$ErrorActionPreference = "Stop"

# Navigate to project root
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

Write-Host "=== Webull Trading Bot ===" -ForegroundColor Cyan

# Check .env exists
if (-not (Test-Path ".env")) {
    Write-Host "ERROR: .env file not found. Copy .env.example to .env and fill in your credentials." -ForegroundColor Red
    exit 1
}

# Check Python
$pythonVersion = python --version 2>&1
Write-Host "Python: $pythonVersion" -ForegroundColor Gray

# Check dependencies
Write-Host "Checking dependencies..." -ForegroundColor Gray
pip install -r requirements.txt --quiet

# Start the bot
Write-Host "Starting bot on port $env:BOT_PORT (default 9100)..." -ForegroundColor Green
python webull_trading_api.py
