# Layered on the image already in the registry, because the original build
# files were never committed. Everything here is additive: a parameterised
# entrypoint, audio, and the current harness so a fresh pod plays without
# anything being copied onto the PVC first.
ARG BASE=192.168.1.187:30500/cogbench:20260904-slacker1

# ---------------------------------------------------------------- ffmpeg
# Built here rather than taken from apt, tuned for the one machine that runs
# it: a Ryzen 7 5800X (Zen 3, AVX2, no AVX-512). Distro builds target a
# decade-old baseline and cannot use AVX2 paths at all, and the encoders in
# this pod are the hottest thing on the node -- three x11grab captures and a
# 60fps x264 each.
#
# This makes the image non-portable ON PURPOSE. -march=native bakes in znver3
# instructions, so the result runs on this node and will crash with an illegal
# instruction anywhere older. BuildKit runs on the same node, which is the only
# reason `native` is meaningful here; move the builder and this must change to
# an explicit -march=znver3.
FROM ${BASE} AS ffbuild
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential nasm yasm pkg-config git ca-certificates \
      libpulse-dev libxcb1-dev libxcb-shm0-dev libxcb-xfixes0-dev \
      libx11-dev libxext-dev libxfixes-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

ENV CFLAGS="-O3 -march=native -mtune=native -mavx2 -pipe -fno-semantic-interposition"
ENV LDFLAGS="-O3 -march=native"

# x264 from source too: linking a generic libx264 into a native ffmpeg would
# leave the actual encoder -- the expensive part -- unoptimised.
RUN git clone --depth 1 https://code.videolan.org/videolan/x264.git /tmp/x264 \
 && cd /tmp/x264 \
 && ./configure --prefix=/opt/ff --enable-static --enable-pic --disable-cli \
      --extra-cflags="$CFLAGS -flto" --extra-ldflags="$LDFLAGS -flto" \
 && make -j"$(nproc)" && make install

# --enable-lto is attempted and not required: ffmpeg's LTO support is fragile
# across versions, and an image that fails to build is worse than one that is a
# few percent slower. The second configure drops only that flag.
RUN git clone --depth 1 --branch n7.1 https://git.ffmpeg.org/ffmpeg.git /tmp/ffmpeg \
 && cd /tmp/ffmpeg \
 && PKG_CONFIG_PATH=/opt/ff/lib/pkgconfig \
    ./configure --prefix=/opt/ff --enable-gpl --enable-version3 \
      --enable-libx264 --enable-libpulse --enable-libxcb \
      --enable-libxcb-shm --enable-libxcb-xfixes \
      --disable-debug --disable-doc --disable-ffplay --disable-ffprobe \
      --enable-lto \
      --extra-cflags="$CFLAGS -I/opt/ff/include" \
      --extra-ldflags="$LDFLAGS -L/opt/ff/lib" \
 || PKG_CONFIG_PATH=/opt/ff/lib/pkgconfig \
    ./configure --prefix=/opt/ff --enable-gpl --enable-version3 \
      --enable-libx264 --enable-libpulse --enable-libxcb \
      --enable-libxcb-shm --enable-libxcb-xfixes \
      --disable-debug --disable-doc --disable-ffplay --disable-ffprobe \
      --extra-cflags="$CFLAGS -I/opt/ff/include" \
      --extra-ldflags="$LDFLAGS -L/opt/ff/lib" \
 && make -j"$(nproc)" && make install

# ---------------------------------------------------------------- statmind
# The reader was an opaque binary inside the base image, built by something that
# was never committed -- so the addresses it used could not be changed without
# finding whoever had the toolchain. It is built here instead, from the source
# next to this repository, which is what makes a build-table change deployable.
#
# Built FROM the base rather than a rust image on purpose: the result has to run
# in that image, and a builder on a different distro links against a glibc the
# runtime may not have.
FROM ${BASE} AS smbuild
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --profile minimal --default-toolchain stable
COPY statmind/ /src/statmind/
WORKDIR /src/statmind
RUN . "$HOME/.cargo/env" && cargo build --release --locked \
 && strip target/release/statmind

# ---------------------------------------------------------------- SDL shim
# The other half of the matched pair. The reader above and this DLL exchange
# StatmindLuigiStatus by memory layout, so a version skew between them is not a
# version skew -- it is the reader indexing into fields the shim never wrote.
# It was cross-built by hand on a laptop (harness/build-sdl.sh) and copied onto
# the PVC, which is exactly how it ended up months behind: a 28-byte struct
# against a 44-byte reader, so `map_object` resolved to the census magic that
# happened to sit at offset 28 and every cell read came back EFAULT.
#
# Building it here makes that unrepresentable. Same tree, same commit, same
# image as the reader, and the entrypoint installs it over the PVC copy on
# every start.
#
# The flags mirror build-sdl.sh. Two of them are load-bearing and neither is
# obvious:
#
#   * -shared stays OUT of the configure-time LDFLAGS. configure links test
#     *executables*; with -shared those links fail and configure quietly
#     decides the features they probed for are missing. libtool passes -shared
#     itself when it actually links the DLL.
#   * -fpermissive and the -Wno-* set. This is 2009 code and GCC 14 promoted
#     several of its idioms from warnings to hard errors. Ubuntu 24.04 cross-
#     compiles with GCC 13, which still only warns, so these are currently
#     redundant -- they are here so that the next base bump is a rebuild rather
#     than an archaeology session. GCC ignores a -Wno- it does not recognise.
FROM ${BASE} AS sdlbuild
USER root
# No DirectX SDK: mingw-w64 ships ddraw.h/dsound.h/dinput.h and the matching
# import libs, so --enable-directx needs nothing beyond the cross toolchain.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential autoconf \
      gcc-mingw-w64-i686 g++-mingw-w64-i686 binutils-mingw-w64-i686 \
 && rm -rf /var/lib/apt/lists/*

ENV SDL_HOST=i686-w64-mingw32
ENV CC=i686-w64-mingw32-gcc \
    CXX=i686-w64-mingw32-g++ \
    WINDRES=i686-w64-mingw32-windres \
    AR=i686-w64-mingw32-ar \
    RANLIB=i686-w64-mingw32-ranlib \
    STRIP=i686-w64-mingw32-strip
ENV CFLAGS="-O2 -D_WIN32_WINNT=0x0501 -static-libgcc -fpermissive \
-Wno-incompatible-pointer-types -Wno-implicit-function-declaration \
-Wno-int-conversion -Wno-implicit-int -Wno-return-mismatch \
-Wno-deprecated-non-prototype -Wno-builtin-declaration-mismatch"
ENV CXXFLAGS="${CFLAGS}"
# Runtime libs only; see the -shared note above. --subsystem,windows matches
# the DLL Cogmind was already loading.
ENV LDFLAGS="-static-libgcc -Wl,--subsystem,windows"
ENV LIBS="-ldxguid -lddraw -ldsound -ldinput -luser32 -lgdi32 -lwinmm"

COPY sdl/ /src/sdl/
WORKDIR /src/sdl
# What autogen.sh does, minus its `(cd test; sh autogen.sh)` tail. test/ is not
# in the build context, and with it absent that line does not fail -- the cd
# fails, the sh does not, and autogen.sh re-executes itself in the root
# directory forever.
RUN cat acinclude/* >aclocal.m4 && autoconf

RUN set -eux; \
    mkdir -p build-win32; cd build-win32; \
    ../configure \
      --host="$SDL_HOST" \
      --prefix=/src/sdl/build-win32/dist \
      --enable-shared \
      --disable-static \
      --enable-directx \
      --disable-assembly \
      --disable-stdio-redirect; \
# `make depend` writes one rule per object naming only its .c file, and the
# hand-written rule in Makefile.in covers SDL.lo against three of the four
# statmind headers -- statmind_blit.h and the three video files that include it
# are tracked by nothing. Editing a header and re-running make can therefore
# produce a DLL with the old struct in it, which is the precise failure this
# stage exists to prevent. Here the tree is unpacked fresh into an empty layer
# every time the context changes, so there is no object to be stale; the touch
# is insurance against a build directory ever reaching the context anyway.
    touch ../src/SDL.c ../src/video/SDL_video.c ../src/video/SDL_surface.c \
          ../src/video/SDL_stretch.c; \
    make -j"$(nproc)"; \
    dll=$(find . -name SDL.dll -print -quit); \
    test -n "$dll"; \
    "$STRIP" --strip-unneeded "$dll"; \
    install -D -m 0644 "$dll" /out/SDL.dll

# The layout check. Building both halves here stops them drifting in *time*;
# it does nothing about the pointer this tree is checked out at, and the two
# are not the same problem. The submodule commit recorded in StatMind lagged
# the shim by two commits, and at that commit StatmindLuigiStatus stops after
# struct_addr -- 24 bytes, no map_object, no view_origin. Such a DLL still
# exports g_statmind_luigi, so the export check below waves it through, it
# installs itself over the PVC copy, and the reader then indexes past the end
# of the struct into the next global. That is the original bug, rebuilt daily
# and automatically.
#
# So assert the layout, against the reader's own numbers rather than a third
# copy of them: LS_* in common.rs is the offset table statmind actually reads
# with, and offsetof is what the shim actually publishes. A field that moves,
# or one the shim does not have at all, is a build failure here instead of an
# EFAULT mid-run.
COPY statmind/src/common.rs /src/reader-offsets.rs
RUN set -eux; \
    cd build-win32; \
    { echo '#include <stddef.h>'; \
      echo '#include "statmind_ipc.h"'; \
      for pair in MODULE_BASE:module_base STRUCT_ADDR:struct_addr \
                  BUILD_STAMP:build_stamp MAP_OBJECT:map_object \
                  PLAYER_REC:player_rec VIEW_ORIGIN:view_origin; do \
        off=$(sed -n "s/^const LS_${pair%%:*}: *usize *= *\([0-9]*\);.*/\1/p" \
              /src/reader-offsets.rs); \
        test -n "$off" || { echo "no LS_${pair%%:*} in common.rs" >&2; exit 1; }; \
        echo "_Static_assert(offsetof(StatmindLuigiStatus, ${pair#*:}) == $off,"; \
        echo "  \"${pair#*:} is not where the reader looks for it\");"; \
      done; \
    } >layout_check.c; \
    i686-w64-mingw32-gcc -fsyntax-only -I include -I ../include -I ../src \
      layout_check.c

# Two ways this build can succeed and still ship something useless, both of
# them silent until the game is up and a run has already been lost.
#
#   * The shim compiled out. SDL.c includes statmind_ipc.h, which defines the
#     mailbox the reader scans for; a DLL without those symbols loads fine,
#     plays fine, and leaves LuigiAI off with nothing in any log to say why.
#   * A dependency Wine cannot satisfy. The game directory holds no
#     libwinpthread-1.dll or libgcc_s_dw2-1.dll, so importing one means
#     COGMIND.exe does not start at all. -static-libgcc is what prevents this,
#     and whether it is enough depends on the threading model apt happened to
#     select for the cross compiler -- which a base bump can change.
RUN set -eux; \
    i686-w64-mingw32-objdump -p /out/SDL.dll >/tmp/pe.txt; \
    grep -q g_statmind_luigi /tmp/pe.txt \
      || { echo "SDL.dll exports no statmind mailbox -- the shim did not compile in" >&2; \
           exit 1; }; \
    ! grep -qiE 'DLL Name: (libwinpthread|libgcc_s)' /tmp/pe.txt \
      || { echo "SDL.dll imports a runtime the game directory does not have" >&2; \
           grep -i 'DLL Name' /tmp/pe.txt >&2; exit 1; }; \
    grep -i 'DLL Name' /tmp/pe.txt

FROM ${BASE}

# The tuned ffmpeg goes in front of the distro one on PATH, so every sidecar
# picks it up without any of them naming a path.
COPY --from=ffbuild /opt/ff /opt/ff
ENV PATH="/opt/ff/bin:${PATH}"

USER root
# There is no sound device in the container, so Wine gets a PulseAudio daemon
# with a null sink and the audio sidecar records that sink's monitor. Wine
# already ships winepulse.so; this is the other half.
#
# ffmpeg comes from Ubuntu rather than a dedicated image because the obvious
# ffmpeg images are built without libpulse -- `-f pulse` fails outright with
# "Unknown input format". Using one image for all three containers also drops a
# docker.io dependency from the deploy.
# The i386 packages are the ones that matter: COGMIND.exe is 32-bit, so it
# loads the 32-bit winepulse, which needs 32-bit libpulse. Without it Wine
# silently falls back to ALSA, ALSA looks for its own 32-bit pulse plugin,
# does not find that either, and the game initialises audio into nothing --
# which is exactly what the logs showed.
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      pulseaudio pulseaudio-utils ffmpeg \
      libpulse0:i386 libasound2-plugins:i386 \
 && rm -rf /var/lib/apt/lists/*
COPY cogbench-entrypoint /usr/local/bin/cogbench-entrypoint
COPY harness/ /opt/cogbench/
# Replaces the base image's copy. The reader and the patched SDL.dll publish and
# consume the same StatmindLuigiStatus, so they are a matched pair: deploying
# one without the other has the reader reading fields the shim never wrote.
COPY --from=smbuild /src/statmind/target/release/statmind /usr/local/bin/statmind
# The other half, from the same tree in the same build. It cannot be used from
# here -- Wine loads it from beside COGMIND.exe, which is on the PVC -- so the
# entrypoint installs it there on every start. That is the step that makes the
# pair a pair rather than two files that agreed once.
COPY --from=sdlbuild /out/SDL.dll /opt/cogbench/SDL.dll
# The file capability is not decoration -- it is the only route by which the
# reader ever gets CAP_SYS_PTRACE. The pod adds SYS_PTRACE, but under
# runAsUser 1000 with no ambient capabilities that only puts it in the bounding
# set; a uid-1000 process starts with an empty effective set and stays there
# unless the binary it exec'd carries the capability itself. The node runs yama
# ptrace_scope=2, so same-uid does not help either.
#
# The base image's opaque reader had this xattr and nobody knew, because
# nothing in a committed file mentioned it. Losing it does not look like a
# permissions problem: every process_vm_readv returns EPERM, the magic scan
# reads nothing anywhere, and the reader reports
# "memory magic 0xBADBEEF1/0x79533ED9 not found" -- i.e. it blames the shim.
# scan_linux_magic now tells those two apart; this line is what keeps them from
# arising. Verify with `getcap /usr/local/bin/statmind` after any base bump.
RUN chmod 0755 /usr/local/bin/cogbench-entrypoint /usr/local/bin/statmind \
 && setcap cap_sys_ptrace=ep /usr/local/bin/statmind \
 && getcap /usr/local/bin/statmind | grep -q cap_sys_ptrace \
 && chmod -R a+rX /opt/cogbench
USER 1000
