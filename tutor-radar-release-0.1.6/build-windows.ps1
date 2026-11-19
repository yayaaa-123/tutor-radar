param([string]$Python = '.venv/Scripts/python.exe', [string]$BasemapDirectory = '')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$pythonExe = (Resolve-Path -LiteralPath $Python).Path
$build = Join-Path $root '.release-build'
$backend = Join-Path $build 'radar-backend'
$key = Join-Path $root 'amap-beta.json'
$basemap = $null
if ($BasemapDirectory) {
    $directory = (Resolve-Path -LiteralPath $BasemapDirectory).Path
    $settings = [ordered]@{
        js_key = [IO.File]::ReadAllText((Join-Path $directory 'amap-js.key')).Trim()
        security_code = [IO.File]::ReadAllText((Join-Path $directory 'amap-js-security-code.txt')).Trim()
        style_id = [IO.File]::ReadAllText((Join-Path $directory 'amap-js-style-id.txt')).Trim()
    }
    foreach ($value in $settings.Values) {
        if ($value -notmatch '^[0-9a-fA-F]{32}$') { throw 'Invalid basemap configuration.' }
    }
    [IO.Directory]::CreateDirectory($build) | Out-Null
    $basemap = Join-Path $build 'amap-basemap.json'
    $settings | ConvertTo-Json -Compress | Set-Content -LiteralPath $basemap -Encoding utf8
}
if (-not (Test-Path -LiteralPath $key -PathType Leaf)) {
    throw 'amap-beta.json is required for a map-enabled release.'
}

Push-Location $root
try {
    $env:PYTHONPATH = Join-Path $root 'src'
    & $pythonExe -c "import json; from pathlib import Path; from tutor_monitor.map_beta import validate_map_beta; validate_map_beta(json.loads(Path('amap-beta.json').read_text(encoding='utf-8-sig')))"
    if ($LASTEXITCODE) { throw 'Invalid map configuration.' }
    $db = & $pythonExe -c "from tutor_monitor.wechat_loader import upstream_status; print(upstream_status()['db_module'])"
    if ($LASTEXITCODE) { throw 'Install the pinned WeChat reader before building.' }
    $freeze = @('--noconfirm', '--onedir', '--name', 'radar-backend', '--paths', 'src',
        '--distpath', $build, '--workpath', (Join-Path $build 'work'), '--specpath', $build,
        '--copy-metadata', 'wechatauto-replica', '--add-data', "$db;wechatauto",
        '--hidden-import', 'cryptography.hazmat.primitives.ciphers',
        '--hidden-import', 'zstandard', '--hidden-import', 'psutil', 'radar_backend.py')
    & $pythonExe -m PyInstaller @freeze
    if ($LASTEXITCODE) { throw 'Backend build failed.' }
    & (Join-Path $backend 'radar-backend.exe') self-test
    if ($LASTEXITCODE) { throw 'Bundled WeChat reader self-test failed.' }
    $override = Join-Path $build 'tauri.release.json'
    $resources = @{}
    $resources[$backend.Replace('\','/')] = 'backend/'
    $resources[$key.Replace('\','/')] = 'amap-beta.json'
    if ($basemap) { $resources[$basemap.Replace('\','/')] = 'amap-basemap.json' }
    @{ bundle = @{ resources = $resources } } | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $override -Encoding utf8
    Push-Location (Join-Path $root 'dashboard')
    try {
        npm run tauri -- build --config $override
        if ($LASTEXITCODE) { throw 'Windows installer build failed.' }
    } finally { Pop-Location }
} finally { Pop-Location }
