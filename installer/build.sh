#!/usr/bin/env bash
# Builds dist/QBSync-Setup-<version>.exe on Linux (or WSL/macOS) with NSIS.
# Needs: python3 + pip, curl, makensis (apt install nsis / brew install makensis).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
VERSION=$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('qbsync/__init__.py').read())[1])")

# 32-bit CPython for Windows (python-build-standalone, the build uv uses). 32-bit matches the
# QuickBooks SDK request processor that every QuickBooks Desktop version registers.
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250409/cpython-3.12.10+20250409-i686-pc-windows-msvc-install_only_stripped.tar.gz"
PY_SHA256="77f7a21fe01c1e437e8ac7ba01299ff0647071e86449f6fdf824e58d0d019e42"

CACHE=$ROOT/build/cache
STAGE=$ROOT/build/stage
SITE=$STAGE/python/Lib/site-packages
mkdir -p "$CACHE" dist
PY_TGZ=$CACHE/$(basename "$PY_URL")
[ -f "$PY_TGZ" ] || curl -fsSL -o "$PY_TGZ" "$PY_URL"
echo "$PY_SHA256  $PY_TGZ" | sha256sum -c -

rm -rf "$STAGE" "$ROOT/build/wheel"
mkdir -p "$STAGE"
tar xzf "$PY_TGZ" -C "$STAGE"          # -> $STAGE/python/...

python3 -m pip wheel -q --no-deps "$ROOT" -w "$ROOT/build/wheel"
python3 -m pip install -q --target "$SITE" --platform win32 --python-version 3.12 --implementation cp \
    --abi cp312 --only-binary=:all: --no-compile --no-deps \
    -r installer/requirements-win32.txt "$ROOT"/build/wheel/qbsync-*.whl

# Trim things the app never uses.
rm -rf "$SITE/PyWin32.chm" "$SITE/pythonwin" "$SITE/bin" "$STAGE/python/Lib/idlelib" \
       "$STAGE/python/Lib/tkinter" "$STAGE/python/Lib/turtledemo" "$STAGE/python/tcl" \
       "$STAGE/python/Lib/test" "$STAGE/python/DLLs/_test"*.pyd "$STAGE/python/DLLs/_tkinter.pyd"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +

cp README.md "$STAGE/README.md"

makensis -V2 -DVERSION="$VERSION" -DSTAGE="$STAGE" -DOUTDIR="$ROOT/dist" installer/qbsync.nsi
ls -la "dist/QBSync-Setup-$VERSION.exe"
sha256sum "dist/QBSync-Setup-$VERSION.exe" | tee "dist/QBSync-Setup-$VERSION.exe.sha256"
