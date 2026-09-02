#!/bin/bash
# Command Center - macOS setup. Run it from Terminal:
#
#     bash ~/Downloads/CommandCenter-Setup-Mac/install.sh
#
# Why Terminal and not a double-click: a file that came from a browser
# download is quarantined, and double-clicking it in Finder takes the
# "open a document" path, which Gatekeeper hard-blocks (macOS 15 removed the
# old right-click-Open escape). Running it as `bash <path>` is the RUN path,
# which Gatekeeper does not police - so this is the calm way in, not the
# scary one. Verified against Apple DTS guidance.
#
# What it does, all inside your home folder, no admin password:
#   0. check the Mac is supported (Apple Silicon, macOS 14+)
#   1. install uv (userland, no Xcode) and let it provision Python 3.12
#   2. build the app environment and install the app + pinned libraries
#   3. run the setup wizard (name, AI key, courses, calendar)
#   4. pre-download the search model so the first question is instant
#   5. GENERATE "Command Center.app" in ~/Applications
#
# Nothing here needs Xcode. The script never calls bare `python3`/`pip3`/`git`,
# because on a Mac without the Command Line Tools those pop a multi-GB
# "install developer tools" dialog.

set -euo pipefail

APP="$HOME/Library/Application Support/CommandCenter"   # engine, venv, data
MATERIALS="$HOME/Command Center"                        # course files (visible)
BIN="$APP/bin"
BUNDLE="$HOME/Applications/Command Center.app"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$APP"
# Every command's real output goes here. Without it the friend can only report
# "it said it failed", because the friendly one-liners below deliberately hide
# the underlying tool's error - and there is nobody sitting next to them to
# re-run it verbosely.
LOG="$APP/install.log"
: > "$LOG"

say()  { printf '  %s\n' "$1"; }
die()  {
    printf '\n!! %s\n\n' "$1" >&2
    printf '   Details were saved to:\n   %s\n\n' "$LOG" >&2
    printf '   Send that file to whoever gave you this and they can tell what happened.\n\n' >&2
    exit 1
}

printf '\n==================== Command Center Setup ====================\n\n'

# ---- 0. supported Mac? (fail in 2 seconds, not mid-download) ----------
# BOTH architectures work. The search engine runs on ONNX Runtime, not
# PyTorch - torch is what used to make Intel Macs impossible (no macOS
# x86_64 build since 2024). Each arch gets the pin set that actually has
# wheels for it, verified package by package.
ARCH="$(uname -m)"
OSVER="$(sw_vers -productVersion)"
OSMAJOR="${OSVER%%.*}"

case "$ARCH" in
  arm64)
    LOCK="requirements-lock.txt"; MIN_OS=14; ARCHNAME="Apple Silicon" ;;
  x86_64)
    LOCK="requirements-lock-macos-intel.txt"; MIN_OS=13; ARCHNAME="Intel" ;;
  *)
    die "Unrecognized Mac processor ($ARCH). Command Center supports Apple Silicon and Intel Macs." ;;
esac

if [ "$OSMAJOR" -lt "$MIN_OS" ]; then
    die "Command Center needs macOS $MIN_OS or newer on an $ARCHNAME Mac - you have $OSVER.
System Settings > General > Software Update will fix this."
fi
say "Mac looks good ($ARCHNAME, macOS $OSVER)"

for f in "$LOCK" launch.py browser-extension; do
    [ -e "$HERE/$f" ] || die "Missing $f next to this script - unzip the whole folder and try again."
done

mkdir -p "$BIN" "$MATERIALS" "$HOME/Applications"

# ---- 1. uv + Python 3.12 (no admin, no Xcode) -------------------------
export UV_UNMANAGED_INSTALL="$BIN"          # keep uv here; don't touch their shell
export UV_PYTHON_INSTALL_DIR="$APP/python"  # keep Python here too
if [ ! -x "$BIN/uv" ]; then
    say "Installing the setup tool (uv)..."
    curl -LsSf https://astral.sh/uv/install.sh 2>>"$LOG" | sh >>"$LOG" 2>&1 \
        || die "Could not download the setup tool. Check your internet connection."
fi
UV="$BIN/uv"
[ -x "$UV" ] || die "The setup tool did not install correctly."

say "Getting Python 3.12 (this is ours alone - your Mac is untouched)..."
"$UV" python install 3.12 >>"$LOG" 2>&1 || die "Could not install Python."
PY="$("$UV" python find 3.12 2>>"$LOG")" || PY=""
[ -n "$PY" ] && [ -x "$PY" ] || die "Could not locate the Python we just installed."

# ssl is mandatory (every API call needs it); tkinter is optional - the
# launcher runs without its status window if the build has no Tk.
"$PY" -c 'import ssl' >>"$LOG" 2>&1 || die "The Python we installed cannot make secure connections."

# ---- 2. app environment + libraries -----------------------------------
# REUSE an existing environment. `uv venv` does not overwrite one, it
# FAILS ("A virtual environment already exists at: ..."), and with the
# || die below that killed every re-run at step 2 - so the update path
# was broken for anyone who already had the app. Clearing it instead
# would be worse: a 150 MB re-download to change one wheel. The
# Windows installer already guards this the same way.
VPY="$APP/venv/bin/python"
if [ -x "$VPY" ]; then
    say "Reusing the existing app environment..."
else
    say "Creating the app environment..."
    "$UV" venv --python "$PY" "$APP/venv" >>"$LOG" 2>&1 || die "Could not create the app environment."
fi

say "Downloading the AI libraries (~150 MB). This is the long part - one time only..."
WHEEL=""
for w in "$HERE"/schoolhub-*.whl; do
    [ -e "$w" ] && WHEEL="$w" && break
done
[ -n "$WHEEL" ] || die "The app file (schoolhub-*.whl) is missing - unzip the whole folder and try again."
# --reinstall-package: the app version does not change between builds,
# and uv treats a same-version wheel as already satisfied ("Checked 1
# package"), so re-running this to UPDATE silently installed nothing and
# left the friend on old code. Only the app is forced; the ~81 pinned
# dependencies are still skipped, so an update stays fast.
"$UV" pip install --python "$VPY" --only-binary=:all: --reinstall-package schoolhub \
     -r "$HERE/$LOCK" "$WHEEL" >>"$LOG" 2>&1 \
     || die "Could not install the AI libraries. If your internet dropped, just run this again."

# ---- 3. runtime files we keep alongside the app ------------------------
# cp -X: do NOT carry the quarantine flag over from the downloaded zip.
cp -X "$HERE/launch.py" "$APP/launch.py"
rm -rf "$APP/browser-extension"
cp -RX "$HERE/browser-extension" "$APP/browser-extension"

# ---- 4. the setup wizard (interactive - this is why we need Terminal) --
printf '\n-------- Let'"'"'s set up your courses and AI --------\n\n'
"$VPY" -m brain init --config "$APP/config.toml" --materials "$MATERIALS"

# ---- 5. pre-download the search model ---------------------------------
# Warm it through the app's OWN embedder. This used to import
# sentence_transformers, which was removed in the ONNX swap - so the import
# failed, the || branch swallowed it, and every friend's FIRST question
# silently stalled on a 130 MB download (or failed outright offline).
say "Downloading the search model (~130 MB, one time)..."
"$VPY" -c "from brain.embeddings import OnnxBgeEmbedder; OnnxBgeEmbedder('BAAI/bge-small-en-v1.5').embed_query('warm up')" >>"$LOG" 2>&1 \
    || say "(skipped - it will download the first time you ask a question)"

# ---- 6. GENERATE the .app -------------------------------------------
# Built here with mkdir + heredocs and never copied from the downloaded zip:
# cp/ditto carry com.apple.quarantine, and a quarantined bundle is exactly
# what triggers the "unidentified developer" wall. A locally generated one is
# never assessed by Gatekeeper, so it just opens - forever, no warnings.
say "Creating your Command Center app..."
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>Command Center</string>
  <key>CFBundleDisplayName</key>     <string>Command Center</string>
  <key>CFBundleIdentifier</key>      <string>local.commandcenter.app</string>
  <key>CFBundleVersion</key>         <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>CFBundleExecutable</key>      <string>CommandCenter</string>
  <key>LSMinimumSystemVersion</key>  <string>$MIN_OS.0</string>
  <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

cat > "$BUNDLE/Contents/MacOS/CommandCenter" <<EOF
#!/bin/sh
# Command Center launcher. A shell script (not a compiled binary) needs no
# code signature on Apple Silicon, so this bundle works with zero build tools.
exec "$APP/venv/bin/python" "$APP/launch.py"
EOF
chmod +x "$BUNDLE/Contents/MacOS/CommandCenter"

# Belt and braces: strip any quarantine that came along for the ride.
xattr -dr com.apple.quarantine "$BUNDLE" 2>/dev/null || true
xattr -dr com.apple.quarantine "$APP"    2>/dev/null || true

printf '\n==================== Almost done! ====================\n'
cat <<EOF

 One manual step - load the browser helper, which keeps your
 deadlines and grades syncing automatically:

   1. Open Chrome and go to:   chrome://extensions
   2. Turn ON "Developer mode" (switch, top right)
   3. Click "Load unpacked" and choose this folder:
        $APP/browser-extension

 Then open "Command Center" from your Applications folder
 (drag it to your Dock to keep it handy).

 Your course files go here - drop PDFs and slides in, and the app
 reads them automatically next time you open it:
        $MATERIALS

 Note: the helper only works in Chrome. Use whatever browser you
 like day to day, just log into OAKS in Chrome once.

 If anything ever misbehaves, run the built-in checkup - it says
 what is wrong and how to fix it (one line, quotes included):

   "\$HOME/Library/Application Support/CommandCenter/venv/bin/python" -m brain doctor

EOF
printf '======================================================\n\n'
