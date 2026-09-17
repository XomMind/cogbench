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
RUN chmod 0755 /usr/local/bin/cogbench-entrypoint \
 && chmod -R a+rX /opt/cogbench
USER 1000
