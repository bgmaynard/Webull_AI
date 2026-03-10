# setup_env.ps1 — Set up environment variables for Webull_AI
# Run this in PowerShell as: .\setup_env.ps1

Write-Host "=== Webull_AI Environment Setup ===" -ForegroundColor Cyan
Write-Host ""

# Anthropic / Claude API Key
$anthropicKey = Read-Host "Enter your ANTHROPIC_API_KEY (starts with sk-ant-...)"
if ($anthropicKey) {
    [System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $anthropicKey, "User")
    $env:ANTHROPIC_API_KEY = $anthropicKey
    Write-Host "ANTHROPIC_API_KEY set successfully." -ForegroundColor Green
} else {
    Write-Host "Skipped ANTHROPIC_API_KEY." -ForegroundColor Yellow
}

# Webull API Key
$webullKey = Read-Host "Enter your WEBULL_API_KEY (or press Enter to skip)"
if ($webullKey) {
    [System.Environment]::SetEnvironmentVariable("WEBULL_API_KEY", $webullKey, "User")
    $env:WEBULL_API_KEY = $webullKey
    Write-Host "WEBULL_API_KEY set successfully." -ForegroundColor Green
} else {
    Write-Host "Skipped WEBULL_API_KEY." -ForegroundColor Yellow
}

# Webull API Secret
$webullSecret = Read-Host "Enter your WEBULL_API_SECRET (or press Enter to skip)"
if ($webullSecret) {
    [System.Environment]::SetEnvironmentVariable("WEBULL_API_SECRET", $webullSecret, "User")
    $env:WEBULL_API_SECRET = $webullSecret
    Write-Host "WEBULL_API_SECRET set successfully." -ForegroundColor Green
} else {
    Write-Host "Skipped WEBULL_API_SECRET." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Done! ===" -ForegroundColor Cyan
Write-Host "Variables are set permanently for your user account." -ForegroundColor White
Write-Host "Restart PowerShell or open a new terminal for changes to take effect." -ForegroundColor White
Write-Host ""

# Verify
Write-Host "Current values:" -ForegroundColor Cyan
Write-Host "  ANTHROPIC_API_KEY = $( if ($env:ANTHROPIC_API_KEY) { $env:ANTHROPIC_API_KEY.Substring(0,10) + '...' } else { '(not set)' })"
Write-Host "  WEBULL_API_KEY    = $( if ($env:WEBULL_API_KEY) { '(set)' } else { '(not set)' })"
Write-Host "  WEBULL_API_SECRET = $( if ($env:WEBULL_API_SECRET) { '(set)' } else { '(not set)' })"
