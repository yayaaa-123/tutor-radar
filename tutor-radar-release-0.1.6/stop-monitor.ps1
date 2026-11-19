$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot 'monitor.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'Tutor monitor is not running (no PID file).'
    exit 0
}
$monitorPid = [int](Get-Content -Raw -LiteralPath $pidFile)
$process = Get-Process -Id $monitorPid -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $monitorPid
    $process.WaitForExit(5000)
}
Remove-Item -LiteralPath $pidFile -Force
Write-Output 'Tutor monitor stopped.'

