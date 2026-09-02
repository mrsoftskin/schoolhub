# install.ps1 - Command Center one-time setup for a friend.
# No admin needed. Uses a SIGNED python.org interpreter so Windows Smart App
# Control never blocks it (uv's Python is unsigned and would be blocked).
#
# Run via install.bat (double-click). Steps:
#   A. copy the app into %LOCALAPPDATA%\SchoolHub
#   B. find or silently install signed Python 3.12.10 (per-user, no UAC)
#   C. make a plain venv (stdlib, not uv)
#   D. install the app + pinned libraries (~1 GB, one time)
#   E. run the setup wizard (name, AI key, courses, calendar)
#   F. pre-download the AI model so the first chat is instant
#   G. make Desktop + Start-Menu shortcuts, print the browser-helper step

param(
    # Where to install. Default is the real per-user location; a test run points
    # this at a scratch folder so nothing touches an existing install.
    [string]$AppHome = (Join-Path $env:LOCALAPPDATA 'SchoolHub'),
    # Reuse this Python instead of finding/installing a signed one (for a local
    # test where you already have a 3.12; skips the python.org download).
    [string]$PythonExe = '',
    # Skip creating Desktop/Start-Menu shortcuts (for a test run).
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Say($m) { Write-Host "  $m" -ForegroundColor Cyan }

# PowerShell 5.1 turns ANY stderr output from a native .exe into a terminating
# error while $ErrorActionPreference is 'Stop' - pip and sentence-transformers
# both write harmless notices to stderr, which aborted this installer right
# before it made the shortcut. Run native tools with that suppression off and
# judge success by the real exit code instead.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$What, [switch]$Tolerate)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0 -and -not $Tolerate) {
        throw "$What failed (exit code $LASTEXITCODE)."
    }
}

Write-Host ""
Write-Host "==================== Command Center Setup ====================" -ForegroundColor Green
Say "Installing to: $AppHome"

# Safety: never install on top of an UNRELATED folder that happens to share the
# name (a real collision was found on the author's machine: another tool owned
# %LOCALAPPDATA%\CommandCenter and kept credentials there). Only an empty
# folder, or a previous install of this app, is an acceptable target.
if (Test-Path $AppHome) {
    $existing = @(Get-ChildItem -Force $AppHome -ErrorAction SilentlyContinue)
    $ours = (Test-Path (Join-Path $AppHome 'launch.py')) -or
            (Test-Path (Join-Path $AppHome 'config.toml')) -or
            (@(Get-ChildItem (Join-Path $AppHome 'schoolhub-*.whl') -ErrorAction SilentlyContinue).Count -gt 0)
    if ($existing.Count -gt 0 -and -not $ours) {
        throw "$AppHome already exists and contains other files, so this installer will not touch it. Re-run with:  -AppHome <a different folder>"
    }
}

if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    Write-Warning "This looks like an ARM64 PC. The AI libraries may not have ARM builds yet; if the install fails at 'downloading libraries', an Intel/AMD (x64) PC is needed for now."
}

# ---- A. app home + payload ----
New-Item -ItemType Directory -Force -Path $AppHome | Out-Null
Copy-Item (Join-Path $here 'schoolhub-*.whl')       $AppHome -Force
Copy-Item (Join-Path $here 'requirements-lock.txt') $AppHome -Force
Copy-Item (Join-Path $here 'launch.py')             $AppHome -Force
Copy-Item (Join-Path $here 'browser-extension')     $AppHome -Recurse -Force

# ---- B. find or provision a SIGNED Python 3.12 ----
function Get-SignedPy312 {
    $cand = $null
    try { $cand = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null) } catch {}
    if (-not $cand) {
        $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $guess) { $cand = $guess }
    }
    if ($cand -and (Test-Path $cand) -and $cand -notmatch '\\uv\\' -and $cand -notmatch 'python-build-standalone') {
        if ((Get-AuthenticodeSignature $cand).Status -eq 'Valid') { return $cand }
    }
    return $null
}

if ($PythonExe -and (Test-Path $PythonExe)) {
    $py = $PythonExe
    Say "Reusing provided Python (test mode): $py"
} else {
    $py = Get-SignedPy312
}
if (-not $py) {
    Say "Installing Python 3.12.10 (signed by the Python Foundation; no admin prompt)..."
    $url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
    $exe = Join-Path $env:TEMP 'cc-python-3.12.10.exe'
    Invoke-WebRequest -Uri $url -OutFile $exe
    $sig = Get-AuthenticodeSignature $exe
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw "The downloaded Python installer failed its signature check. Aborting for safety."
    }
    Start-Process $exe -Wait -ArgumentList @(
        '/quiet','InstallAllUsers=0','InstallLauncherAllUsers=0','PrependPath=1',
        'Include_launcher=1','Include_pip=1','Include_tcltk=1','Include_test=0',
        'AssociateFiles=0','Shortcuts=0')
    $py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
}
if (-not (Test-Path $py)) { throw "Could not find or install Python 3.12." }
Say "Using Python: $py"

# ---- C. venv (stdlib, NOT uv) ----
$venv = Join-Path $AppHome '.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Say "Creating the app environment..."
    Invoke-Native $py @('-m','venv',$venv) "Creating the app environment"
}
$vpy = Join-Path $venv 'Scripts\python.exe'
Invoke-Native $vpy @('-m','pip','install','--upgrade','pip','--quiet') "Updating pip" -Tolerate

# ---- D. install app + pinned libraries ----
$whl = (Get-ChildItem (Join-Path $AppHome 'schoolhub-*.whl') | Select-Object -First 1).FullName
Say "Downloading the AI libraries (~1 GB). This is the long part - one time only..."
# --force-reinstall on the APP wheel only (--no-deps keeps the ~81 pinned
# dependencies untouched): the app version does not change between
# builds, so pip treats a rebuilt wheel as already satisfied and a
# re-run meant to UPDATE would silently leave the old code in place.
Invoke-Native $vpy @('-m','pip','install','--only-binary=:all:','-r',(Join-Path $AppHome 'requirements-lock.txt'),$whl) "Installing the AI libraries (if this is an ARM PC, that's the cause)"
Invoke-Native $vpy @('-m','pip','install','--force-reinstall','--no-deps',$whl) "Updating Command Center"

# ---- E. setup wizard (interactive) ----
Write-Host ""
Write-Host "-------- Let's set up your courses and AI --------" -ForegroundColor Green
Push-Location $AppHome
Invoke-Native $vpy @('-m','brain','init','--config',(Join-Path $AppHome 'config.toml')) "Setup wizard"
Pop-Location

# ---- F. pre-download the AI model so first chat is instant ----
Say "Downloading the search model (~130 MB, one time)..."
# Warm it through the app's OWN embedder. This used to import
# sentence_transformers, which was REMOVED in the ONNX swap - so the
# import failed, -Tolerate swallowed it, and every Windows friend's
# FIRST question silently stalled on a 130 MB download (or failed
# outright offline). The Mac installer was fixed for this; this one
# was not.
Invoke-Native $vpy @('-c',"from brain.embeddings import OnnxBgeEmbedder; OnnxBgeEmbedder('BAAI/bge-small-en-v1.5').embed_query('warm up')") "Model download" -Tolerate

# ---- G. shortcuts -> signed pythonw + launch.py ----
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }   # fall back to python.exe if no pythonw
function New-CCShortcut($lnk) {
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $AppHome 'launch.py') + '"'
    $s.WorkingDirectory = $AppHome
    $s.IconLocation = $pyw
    $s.Description = 'Command Center'
    $s.Save()
}
if (-not $NoShortcuts) {
    New-CCShortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Command Center.lnk')
    $startmenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    New-CCShortcut (Join-Path $startmenu 'Command Center.lnk')
} else {
    Say "Test mode: skipped Desktop/Start-Menu shortcuts."
}

Write-Host ""
Write-Host "==================== Almost done! ====================" -ForegroundColor Green
Write-Host " One manual step - load the browser helper (syncs your"
Write-Host " deadlines and grades automatically):"
Write-Host ""
Write-Host "   1. Open Chrome and go to:  chrome://extensions"
Write-Host "   2. Turn ON 'Developer mode' (switch, top-right)"
Write-Host "   3. Click 'Load unpacked' and choose this folder:"
Write-Host "        $AppHome\browser-extension" -ForegroundColor Yellow
Write-Host ""
Write-Host " Then double-click the 'Command Center' icon on your Desktop."
Write-Host " (Log into OAKS in Chrome once so it can sync.)"
Write-Host "======================================================" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to finish"
