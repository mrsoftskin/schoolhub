# test-install.ps1 - run the FRIEND installer end-to-end on your own machine,
# into a scratch folder, WITHOUT touching your real Command Center.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\test-install.ps1 -Launch
#
# It stages the release, reuses a Python 3.12 you already have (so it does NOT
# download/install python.org - that part you'd only see on a truly clean box),
# installs into a scratch dir, skips Desktop shortcuts, and (with -Launch) opens
# the app on a TEST port so it never collides with your live app on 8177.
# Your real config, data, and materials are untouched.

param(
    [string]$Scratch = (Join-Path $env:TEMP 'CommandCenter-test'),
    [int]$Port = 8188,
    [switch]$Launch
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "1) Staging the release zip payload..." -ForegroundColor Cyan
Push-Location $repo
& uv run python packaging/make_release.py | Out-Null
Pop-Location
$stage = Join-Path $repo 'dist\CommandCenter-Setup'
if (-not (Test-Path (Join-Path $stage 'install.ps1'))) { throw "Release stage not built." }

Write-Host "2) Finding a Python 3.12 to reuse..." -ForegroundColor Cyan
$py = $null
$uvpy = Join-Path $env:APPDATA 'uv\python\cpython-3.12-windows-x86_64-none\python.exe'
if (Test-Path $uvpy) { $py = $uvpy }
if (-not $py) { try { $py = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null) } catch {} }
if (-not $py -or -not (Test-Path $py)) {
    throw "No Python 3.12 found to reuse. Run the real install.bat to test provisioning instead."
}
Write-Host "   Reusing: $py"

if (Test-Path $Scratch) { Remove-Item -Recurse -Force $Scratch }

Write-Host "3) Running the installer into a scratch folder: $Scratch" -ForegroundColor Green
& (Join-Path $stage 'install.ps1') -AppHome $Scratch -PythonExe $py -NoShortcuts

Write-Host ""
if ($Launch) {
    Write-Host "4) Launching the test app on port $Port..." -ForegroundColor Green
    $env:CC_PORT = "$Port"
    $vpyw = Join-Path $Scratch '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $vpyw)) { $vpyw = Join-Path $Scratch '.venv\Scripts\python.exe' }
    Start-Process $vpyw -ArgumentList ('"' + (Join-Path $Scratch 'launch.py') + '"') -WorkingDirectory $Scratch
    Start-Sleep -Seconds 3
    Start-Process "http://127.0.0.1:$Port"
    Write-Host "   Opened http://127.0.0.1:$Port (your real app on 8177 is untouched)."
} else {
    Write-Host "To launch the test app (port $Port):" -ForegroundColor Yellow
    Write-Host "  `$env:CC_PORT=$Port; & `"$Scratch\.venv\Scripts\pythonw.exe`" `"$Scratch\launch.py`""
}
Write-Host ""
Write-Host "When done, delete the test install with:" -ForegroundColor Yellow
Write-Host "  Remove-Item -Recurse -Force `"$Scratch`""
