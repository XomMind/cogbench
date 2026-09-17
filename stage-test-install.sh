#!/usr/bin/env bash
# Stage a clean Cogmind install plus the patched SDL.dll, so testing never
# touches the live game directory or your own profile.
#
# Copies only; nothing here modifies the source install or the app bundle.
#
#   ./stage-test-install.sh                    stage the newest ingested release
#   RELEASE=beta-17.1-6c96192b9b7a ./stage...  stage a named one
#   GAME_SRC="/path/to/COGMIND (Beta 17.1)" ./stage...   stage any other copy
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASES="${RELEASES:-$HERE/../releases}"

# Ingested releases live one directory per build, each holding the unmodified
# game folder. Newest by mtime unless RELEASE names one.
if [ -z "${GAME_SRC:-}" ]; then
  if [ -n "${RELEASE:-}" ]; then
    release_dir="$RELEASES/$RELEASE"
    [ -d "$release_dir" ] || { echo "no release: $release_dir" >&2; exit 1; }
  else
    release_dir=$(ls -dt "$RELEASES"/*/ 2>/dev/null | head -1 || true)
    [ -n "$release_dir" ] || {
      echo "no releases under $RELEASES; set GAME_SRC to a game folder" >&2; exit 1; }
    release_dir="${release_dir%/}"
  fi
  GAME_SRC=$(find "$release_dir" -maxdepth 2 -name COGMIND.exe -print -quit)
  GAME_SRC="${GAME_SRC%/COGMIND.exe}"
  [ -n "$GAME_SRC" ] || { echo "no COGMIND.exe under $release_dir" >&2; exit 1; }
fi

WRAPPER="${WRAPPER:-/Applications/Cogmind.app}"
DEST="${DEST:-$WRAPPER/Contents/SharedSupport/prefix/drive_c/COGMIND-COGBENCH}"
PROFILE="${PROFILE:-$HOME/Documents/Cogmind-cogbench}"
SDL_DLL="${SDL_DLL:-}"

[ -f "$GAME_SRC/COGMIND.exe" ] || { echo "no COGMIND.exe in: $GAME_SRC" >&2; exit 1; }

# Refuse a build the shim will not recognise. Staging one anyway produces a
# game that launches and plays normally with LuigiAI silently off and stat
# dumps disabled -- a failure that only shows up as the harness timing out
# much later, with nothing pointing back at the executable.
if ! python3 "$HERE/verify_retail.py" "$GAME_SRC/COGMIND.exe" >/dev/null; then
  echo "refusing to stage: $GAME_SRC/COGMIND.exe is not a build the shim supports." >&2
  echo "Add it to statmind_build.h first -- see notes/retail-17.1.md." >&2
  exit 1
fi

echo "staging  $GAME_SRC"
echo "     ->  $DEST"
mkdir -p "$DEST"
rsync -a --delete "$GAME_SRC/" "$DEST/"

if [ -n "$SDL_DLL" ]; then
  [ -f "$SDL_DLL" ] || { echo "no SDL.dll at: $SDL_DLL" >&2; exit 1; }
  # Keep the stock DLL if the release shipped one.
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

Confirm the fixed data addresses on this build (see notes/retail-17.1.md):

  ./snapshot.sh <integrity> <matter>
NEXT
