$ErrorActionPreference = 'Continue'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

& (Join-Path $projectRoot 'stop-monitor.ps1') | Out-Null

$apiPidPath = Join-Path $projectRoot 'dashboard-api.pid'
if (Test-Path -LiteralPath $apiPidPath) {
    $apiProcessId = [int](Get-Content -Raw -LiteralPath $apiPidPath)
    Stop-Process -Id $apiProcessId -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $apiPidPath -Force -ErrorAction SilentlyContinue
}
