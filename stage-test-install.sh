#!/usr/bin/env bash
# Stage a clean Beta 17.1 install plus the patched SDL.dll, so testing never
# touches the live game directory or your own profile.
#
# Copies only; nothing here modifies the source install or the app bundle.
set -euo pipefail

RETAIL="${RETAIL:-/Users/heni/Downloads/COGMIND (Beta 17.1)}"
WRAPPER="${WRAPPER:-/Applications/Cogmind.app}"
DEST="${DEST:-$WRAPPER/Contents/SharedSupport/prefix/drive_c/COGMIND-COGBENCH}"
PROFILE="${PROFILE:-$HOME/Documents/Cogmind-cogbench}"
SDL_DLL="${SDL_DLL:-}"

[ -d "$RETAIL" ] || { echo "no retail copy at: $RETAIL" >&2; exit 1; }

echo "staging  $RETAIL"
echo "     ->  $DEST"
mkdir -p "$DEST"
rsync -a --delete "$RETAIL/" "$DEST/"

if [ -n "$SDL_DLL" ]; then
  [ -f "$SDL_DLL" ] || { echo "no SDL.dll at: $SDL_DLL" >&2; exit 1; }
  # Keep the stock DLL if the retail copy shipped one.
  [ -f "$DEST/SDL.dll" ] && cp "$DEST/SDL.dll" "$DEST/SDL.dll.stock"
  cp "$SDL_DLL" "$DEST/SDL.dll"
  echo "installed patched SDL.dll ($(stat -f%z "$SDL_DLL") bytes)"
else
  echo "NOTE: SDL_DLL not set -- no patched shim installed, LuigiAI will stay off."
fi

mkdir -p "$PROFILE/user"
cat > "$PROFILE/user/advanced.cfg" <<'CFG'
exposeKeybinds=1
jsonScoresheet=1
jsonStatDump=1
CFG
echo "fresh profile at $PROFILE (exposeKeybinds=1 so commands.cfg/keyboard.cfg regenerate)"

cat <<NEXT

Wire the wrapper by hand (it edits Info.plist, so do it yourself):

  Program Name and Path   /COGMIND-COGBENCH/COGMIND.exe
  Program Flags           -customFilePath:"Z:${PROFILE//\//\\}"

Then launch, and:

  ./gen_actions.py --user-dir "$PROFILE/user" -o actions.json
NEXT
