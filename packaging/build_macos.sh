#!/usr/bin/env bash
# Compila "M4 CopyDesk.app" e il .dmg su macOS (Apple Silicon). Uso: packaging/build_macos.sh
# Requisiti: macOS 13+, Python 3.12+ (python3 nel PATH o PYTHON=...). Nessun account Apple a pagamento:
# l'app è firmata ad-hoc (non notarizzata), vedi packaging/LEGGIMI.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname)" == "Darwin" ]] || { echo "Questo script va eseguito su macOS"; exit 1; }
PYTHON="${PYTHON:-python3}"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
ARCH="$(uname -m)"
export M4DESK_VERSION="$VERSION"

echo "== ambiente di build"
rm -rf build/venv-mac build/dist build/work build/dmg
"$PYTHON" -m venv build/venv-mac
build/venv-mac/bin/python -m pip install -q --upgrade pip
build/venv-mac/bin/python -m pip install -q . pyinstaller rumps pillow

echo "== icona"
build/venv-mac/bin/python packaging/make_icon.py build/icon
iconutil -c icns build/icon/M4CopyDesk.iconset -o build/icon/M4CopyDesk.icns

echo "== PyInstaller"
build/venv-mac/bin/pyinstaller packaging/m4desk.spec --noconfirm --distpath build/dist --workpath build/work
APP="build/dist/M4 CopyDesk.app"

echo "== firma ad-hoc (nessun certificato a pagamento)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
plutil -lint "$APP/Contents/Info.plist"

echo "== smoke test dell'app compilata"
packaging/smoke_test.sh "$APP/Contents/MacOS/M4 CopyDesk"

echo "== DMG"
mkdir -p build/dmg
cp -R "$APP" build/dmg/
ln -s /Applications build/dmg/Applications
cp packaging/LEGGIMI.txt build/dmg/LEGGIMI.txt
DMG="build/M4-CopyDesk-${VERSION}-macOS-${ARCH}.dmg"
rm -f "$DMG"
hdiutil create -volname "M4 CopyDesk" -srcfolder build/dmg -ov -format UDZO "$DMG"
hdiutil verify "$DMG"

echo "== verifica contenuto del DMG"
MNT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MNT" >/dev/null
test -x "$MNT/M4 CopyDesk.app/Contents/MacOS/M4 CopyDesk"
M4DESK_HOME="$(mktemp -d)" "$MNT/M4 CopyDesk.app/Contents/MacOS/M4 CopyDesk" --version
hdiutil detach "$MNT" >/dev/null
(cd build && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
echo "== FATTO: $DMG"
cat "$DMG.sha256"
