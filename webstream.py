#!/usr/bin/env python3
"""
Low-latency watch feed for a browser, fanned out to any number of clients.

Why this exists alongside the ffmpeg HTTP muxers: those serve exactly one
client and then exit, they hand out containers a browser will not play, and the
route to a screen was Safari -> OBS -> Twitch, which is seconds of latency for
something being watched on the same LAN as the machine rendering it.

How it works. One ffmpeg grabs X and PulseAudio and writes fragmented MP4 to a
pipe: an initialisation segment (ftyp+moov) followed by a stream of fragments
(moof+mdat). This process keeps the init segment, keeps a short ring of recent
fragments, and gives every HTTP client the init segment followed by live
fragments from wherever the stream currently is. The page appends those to a
MediaSource buffer.

That is the whole trick. There is no segmenting to disk, no playlist, and no
rewind: a client joins at the live edge and stays there. On a LAN the glass to
glass is a few hundred milliseconds, most of it the encoder's own buffer.

    ./webstream.py serve --port 8094
"""

import argparse
import collections
import http.server
import os
import socketserver
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "stream")
PORT = 8094

DISPLAY = os.environ.get("DISPLAY", ":99")
SIZE = os.environ.get("COGBENCH_WEB_SIZE", "1440x1080")
FPS = os.environ.get("COGBENCH_WEB_FPS", "60")
CRF = os.environ.get("COGBENCH_WEB_CRF", "18")
# Keyframes decide how long a new viewer stares at nothing, because a
# MediaSource cannot start decoding mid-GOP. One second is a good trade: any
# shorter and the bitrate climbs for no benefit on a screen that barely moves.
GOP = os.environ.get("COGBENCH_WEB_GOP", str(int(FPS)))


def ffmpeg_argv():
    return [
        "ffmpeg", "-loglevel", "warning",
        "-thread_queue_size", "512",
        "-f", "x11grab", "-draw_mouse", "0",
        "-framerate", FPS, "-video_size", SIZE, "-i", DISPLAY,
        "-thread_queue_size", "512",
        "-f", "pulse", "-i", "cogbench.monitor",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-crf", CRF, "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-g", GOP, "-bf", "0",
        # repeat-headers keeps SPS/PPS in band. It matters less for fMP4 than
        # for TS -- the moov carries them -- but costs nothing and makes the
        # stream self-describing if it is ever remuxed.
        "-x264-params", "keyint=%s:scenecut=0:repeat-headers=1:psy-rd=0:"
                        "aq-mode=0:deblock=-2,-2" % GOP,
        "-af", "aresample=async=1000:min_hard_comp=0.100:first_pts=0",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        # empty_moov + default_base_moof is the combination browsers expect.
        # The flag is default_base_moof, not default_base_is_moof -- the latter
        # is what the spec calls the bit it sets, and ffmpeg rejects the whole
        # movflags string if any one name is wrong, so the muxer never opened;
        # frag_duration sets how often a fragment is emitted, and that is the
        # latency floor. 200ms is below what anyone notices and still large
        # enough that the per-fragment overhead stays small.
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
        "-frag_duration", "200000",
        "-f", "mp4", "pipe:1",
    ]


class Fanout:
    """Reads fMP4 from ffmpeg once and hands it to every listener.

    Boxes are parsed rather than the pipe being split on an arbitrary buffer
    size, because a client has to receive whole boxes in order: half a moof is
    not something a SourceBuffer can be given.
    """

    def __init__(self):
        self.init = b""             # ftyp + moov, replayed to every new client
        self.lock = threading.Lock()
        # A list, not a set: the clients are deques and a deque is unhashable,
        # so a set silently works until the first viewer connects and then
        # raises inside the request handler.
        self.clients = []
        self.proc = None
        self.started = 0.0
        self.restarts = 0
        self.bytes_out = 0

    def add(self, q):
        with self.lock:
            self.clients.append(q)

    def drop(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, chunk):
        with self.lock:
            for q in self.clients:
                # A viewer whose connection has stalled must not hold the
                # stream back for everyone else, so its queue is bounded and
                # the oldest fragment is dropped. Falling behind is recoverable
                # -- MediaSource skips ahead -- and blocking here is not.
                if len(q) >= 60:
                    try:
                        q.popleft()
                    except IndexError:
                        pass
                q.append(chunk)
        self.bytes_out += len(chunk)

    def run(self):
        """Keep one ffmpeg alive and parse its output into whole boxes."""
        while True:
            self.proc = subprocess.Popen(ffmpeg_argv(), stdout=subprocess.PIPE,
                                         stderr=None, bufsize=0)
            self.started = time.time()
            try:
                self._pump(self.proc.stdout)
            except Exception:
                pass
            try:
                self.proc.kill()
            except Exception:
                pass
            self.restarts += 1
            # X or PulseAudio going away (a container restart next door) is the
            # usual reason to land here, and both come back within seconds.
            time.sleep(1.0)

    def _pump(self, out):
        init = b""
        while True:
            head = _read_exactly(out, 8)
            if not head:
                return
            size = struct.unpack(">I", head[:4])[0]
            kind = head[4:8]
            if size == 1:                      # 64-bit extended size
                ext = _read_exactly(out, 8)
                if not ext:
                    return
                size = struct.unpack(">Q", ext)[0]
                body = _read_exactly(out, size - 16)
                box = head + ext + (body or b"")
            else:
                body = _read_exactly(out, size - 8) if size > 8 else b""
                if size > 8 and body is None:
                    return
                box = head + (body or b"")
            if kind in (b"ftyp", b"moov"):
                # The init segment is whatever precedes the first fragment.
                # Held whole so a client arriving an hour from now still gets a
                # decodable stream without waiting for ffmpeg to restart.
                init += box
                if kind == b"moov":
                    self.init = init
                continue
            self.publish(box)


def _read_exactly(f, n):
    buf = b""
    while len(buf) < n:
        b = f.read(n - len(buf))
        if not b:
            return None
        buf += b
    return buf


FAN = Fanout()


class Handler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1, so the live response can be chunked. Python's base handler
    # speaks 1.0 by default, where a body with no Content-Length ends only when
    # the connection closes -- and Safari waits for that end before handing the
    # body to fetch(). On a stream that never ends, the page sits on
    # "connecting" forever while the bytes pile up unseen. Chunked encoding
    # tells it the response is arriving in pieces it may consume now.
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/watch", "/watch.html"):
            return self.sendfile(os.path.join(PAGE, "watch.html"), "text/html")
        if path == "/live.mp4":
            return self.live()
        if path == "/stat":
            import json
            body = json.dumps({
                "clients": len(FAN.clients),
                "init_bytes": len(FAN.init),
                "uptime": round(time.time() - FAN.started, 1),
                "restarts": FAN.restarts,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.send_error(404)

    def sendfile(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def live(self):
        """One viewer: the init segment, then fragments as they are produced."""
        init = FAN.init
        if not init:
            return self.send_error(503, "stream starting")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        q = collections.deque()
        FAN.add(q)
        try:
            self.chunk(init)
            idle = 0.0
            while True:
                if not q:
                    time.sleep(0.01)
                    idle += 0.01
                    # Nothing for this long means the encoder died rather than
                    # the screen being still -- fragments are emitted on a timer.
                    if idle > 30:
                        return
                    continue
                idle = 0.0
                self.chunk(q.popleft())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                     # the tab was closed
        finally:
            FAN.drop(q)

    def chunk(self, data):
        """One HTTP chunk: hex length, CRLF, bytes, CRLF."""
        self.wfile.write(b"%X\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *a):
        pass


def serve(port=PORT):
    threading.Thread(target=FAN.run, daemon=True).start()

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    with Server(("0.0.0.0", port), Handler) as srv:
        print("watch:  http://0.0.0.0:%d/" % port)
        print("stream: http://0.0.0.0:%d/live.mp4" % port)
        srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["serve"])
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    try:
        serve(a.port)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    sys.exit(main() or 0)
