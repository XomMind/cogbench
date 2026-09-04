#!/usr/bin/env bash
# Launch Cogmind under the wrapper's wine, with an isolated profile.
#
# Two things the Sikarugir launcher does that are easy to miss:
#   * DYLD_FALLBACK_LIBRARY_PATH must include Contents/Frameworks, or wineserver
#     fails with "Library not loaded: @rpath/libinotify.0.dylib".
#   * The working directory must be the game directory, or the game dies with
#     "FATAL ERROR: init | Unable to open object data" -- it resolves its data
#     archive relative to cwd.
# And do NOT wrap this in `nohup`: it lives in SIP-protected /usr/bin, so macOS
# strips every DYLD_* variable when spawning through it, which reintroduces the
# first failure with no clue as to why.
set -u
PROFILE="${1:-$HOME/Documents/Cogmind-bench}"
A=/Applications/Cogmind.app/Contents
W="$A/SharedSupport"
G="$W/prefix/drive_c/COGMIND (Beta 17.1)"
WIN_PROFILE="Z:$(printf '%s' "$PROFILE" | sed 's|/|\\|g')"

mkdir -p "$PROFILE/user" "$PROFILE/scores" "$PROFILE/screenshots"
cd "$G" || { echo "no game dir at $G" >&2; exit 1; }
export WINEPREFIX="$W/prefix"
export WINEDEBUG="${WINEDEBUG:--all}"
export DYLD_FALLBACK_LIBRARY_PATH="$A/Frameworks:$W/wine/lib:/usr/local/lib:/usr/lib"
echo "profile: $PROFILE"
echo "     as: $WIN_PROFILE"
"$W/wine/bin/wine" "C:\\COGMIND (Beta 17.1)\\COGMIND.exe" \
  -luigiAi "-customFilePath:$WIN_PROFILE" >"${COGBENCH_WINE_LOG:-/tmp/cogbench-wine.log}" 2>&1 &
echo "wine pid $!"
