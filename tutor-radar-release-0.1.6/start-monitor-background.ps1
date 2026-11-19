$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$stdout = Join-Path $projectRoot 'monitor.stdout.log'
$stderr = Join-Path $projectRoot 'monitor.stderr.log'
$pidFile = Join-Path $projectRoot 'monitor.pid'

if (Test-Path -LiteralPath $pidFile) {
    $oldPid = [int](Get-Content -Raw -LiteralPath $pidFile)
    if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
        throw "Monitor is already running with PID $oldPid"
    }
}

$arguments = @(
    '-m', 'tutor_monitor.cli', 'run',
    '--config', (Join-Path $projectRoot 'config.toml'),
    '--allow-process-read'
)
$process = Start-Process -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru
$process.Id | Set-Content -LiteralPath $pidFile -Encoding ascii
Write-Output "Tutor monitor started with PID $($process.Id)"
