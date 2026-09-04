#!/usr/bin/env bash
# Cross-build the patched SDL 1.2 as a Win32 DLL for Cogmind under Wine.
#
# Adapted from StatMind/SDL-1.2/cross-toolchain.sh (which targeted llvm-mingw on
# Linux) for Homebrew's mingw-w64 on macOS. Two deliberate differences:
#
#   * No DirectX SDK. mingw-w64 ships its own ddraw.h / dsound.h / dinput.h and
#     the matching import libs, so the old -L$HOME/DirectX/Lib/x86 is unnecessary.
#   * `-shared` is kept OUT of the configure-time LDFLAGS. configure links test
#     *executables*; with -shared in LDFLAGS those tests fail and configure
#     silently concludes that features are missing. libtool adds -shared itself
#     at link time.
#
# SDL 1.2.14 is 2009 code and GCC 14+ promoted several of its idioms to hard
# errors, hence -fpermissive and the -Wno-* set.
set -euo pipefail

SDL_DIR="${SDL_DIR:-/Users/heni/genAI/cogbench/StatMind/SDL-1.2}"
BUILD_DIR="${BUILD_DIR:-$SDL_DIR/build-win32}"
HOST=i686-w64-mingw32
JOBS="${JOBS:-$(sysctl -n hw.ncpu)}"

command -v $HOST-gcc >/dev/null || { echo "missing $HOST-gcc -- brew install mingw-w64" >&2; exit 1; }
[ -f "$SDL_DIR/configure.in" ] || { echo "no SDL sources at $SDL_DIR (git submodule update --init?)" >&2; exit 1; }

echo "==> toolchain: $($HOST-gcc --version | head -1)"
echo "==> sysroot:   $($HOST-gcc -print-sysroot)"

export CC="$HOST-gcc"
export CXX="$HOST-g++"
export WINDRES="$HOST-windres"
export AR="$HOST-ar"
export RANLIB="$HOST-ranlib"
export STRIP="$HOST-strip"

export CFLAGS="-O2 -D_WIN32_WINNT=0x0501 -static-libgcc \
-fpermissive \
-Wno-incompatible-pointer-types -Wno-implicit-function-declaration \
-Wno-int-conversion -Wno-implicit-int -Wno-return-mismatch \
-Wno-deprecated-non-prototype -Wno-builtin-declaration-mismatch"
export CXXFLAGS="$CFLAGS"
# Runtime libs only. No -shared here; see the note above.
# --subsystem,windows matches the DLL Cogmind was already loading.
export LDFLAGS="-static-libgcc -Wl,--subsystem,windows"
export LIBS="-ldxguid -lddraw -ldsound -ldinput -luser32 -lgdi32 -lwinmm"

cd "$SDL_DIR"
if [ ! -f configure ]; then
  echo "==> generating configure"
  ./autogen.sh
fi

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

if [ ! -f Makefile ] || [ "${RECONFIGURE:-0}" = "1" ]; then
  echo "==> configure"
  ../configure \
    --host=$HOST \
    --prefix="$BUILD_DIR/dist" \
    --enable-shared \
    --disable-static \
    --enable-directx \
    --disable-assembly \
    --disable-stdio-redirect
fi

echo "==> make -j$JOBS"
make -j"$JOBS"

DLL=$(find "$BUILD_DIR" -name "SDL.dll" -o -name "SDL-1-2*.dll" | head -1)
if [ -z "$DLL" ]; then
  echo "build finished but no DLL found; look in $BUILD_DIR/build/.libs" >&2
  exit 1
fi
echo "==> stripping"
$HOST-strip --strip-unneeded "$DLL"

echo
echo "==> built: $DLL"
file "$DLL"
ls -l "$DLL"
echo
echo "Statmind symbols exported (should list the mailbox + status globals):"
$HOST-nm -g --defined-only "$DLL" 2>/dev/null | grep -i statmind || \
  $HOST-objdump -p "$DLL" | grep -iE "statmind|g_ipc" || echo "  (none found -- check the shim compiled in)"
