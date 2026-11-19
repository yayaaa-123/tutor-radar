$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $projectRoot 'src'

function Test-RecordedProcess {
    param([string]$PidPath)
    if (-not (Test-Path -LiteralPath $PidPath)) { return $false }
    $recordedProcessId = [int](Get-Content -Raw -LiteralPath $PidPath)
    return $null -ne (Get-Process -Id $recordedProcessId -ErrorAction SilentlyContinue)
}

function Start-LocalProcess {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$PidPath,
        [string]$StdoutPath,
        [string]$StderrPath
    )
    $process = Start-Process -FilePath $Executable `
        -ArgumentList $Arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -PassThru
    $process.Id | Set-Content -LiteralPath $PidPath -Encoding ascii
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment is missing: $python"
}

if (-not (Get-Process -Name 'Weixin' -ErrorAction SilentlyContinue)) {
    throw 'Open and sign in to WeChat before starting Tutor Radar.'
}

& (Join-Path $projectRoot 'start-monitor-background.ps1') | Out-Null

$apiPidPath = Join-Path $projectRoot 'dashboard-api.pid'
if (-not (Test-RecordedProcess -PidPath $apiPidPath)) {
    Start-LocalProcess `
        -Executable $python `
        -Arguments @('-m', 'tutor_monitor.dashboard_api') `
        -PidPath $apiPidPath `
        -StdoutPath (Join-Path $projectRoot 'dashboard-api.stdout.log') `
        -StderrPath (Join-Path $projectRoot 'dashboard-api.stderr.log')
}
