#!/usr/bin/env python3
"""
Low-latency watch feed for a browser, fanned out to any number of clients.

Why this exists alongside the ffmpeg HTTP muxers: those serve exactly one
client and then exit, they hand out containers a browser will not play, and the
route to a screen was Safari -> OBS -> Twitch, which is seconds of latency for
something being watched on the same LAN as the machine rendering it.

How it works. One ffmpeg grabs X and PulseAudio and writes fragmented MP4 to a
pipe: an initialisation segment (ftyp+moov) followed by a stream of fragments
(moof+mdat). This process keeps the init segment and gives every HTTP client
the init segment followed by complete live
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
CRF = os.environ.get("COGBENCH_WEB_CRF", "14")
# The node is a 16-thread 5800X sitting at around 10% with everything running,
# so the encoder is nowhere near the constraint and the preset can be spent on
# quality instead of speed. `slow` costs perhaps a core more than `veryfast`
# and buys a real reduction in the mush around glyph edges at the same CRF.
PRESET = os.environ.get("COGBENCH_WEB_PRESET", "slow")
ABR = os.environ.get("COGBENCH_WEB_ABR", "256k")
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
        # zerolatency stays. It is what forbids B-frames and both lookaheads,
        # and without it a slower preset would buy its quality back in frames
        # of delay -- which is the one thing this feed exists to avoid.
        "-c:v", "libx264", "-preset", PRESET, "-tune", "zerolatency",
        "-crf", CRF, "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-g", GOP, "-bf", "0",
        # repeat-headers keeps SPS/PPS in band. It matters less for fMP4 than
        # for TS -- the moov carries them -- but costs nothing and makes the
        # stream self-describing if it is ever remuxed.
        # Tuned for a screen of text rather than for video. psy-rd invents
        # detail that reads as noise on flat black; aq-mode spends bits on flat
        # areas, and this screen is mostly flat; the deblocking filter rounds
        # the corners off letters, so it is turned down rather than off. ref
        # and subme are raised because a static screen references its own past
        # almost perfectly and the extra search is nearly free here.
        "-x264-params", "keyint=%s:scenecut=0:repeat-headers=1:psy-rd=0:"
                        "aq-mode=0:deblock=-2,-2:ref=4:subme=9:trellis=2:"
                        "me=umh" % GOP,
        "-af", "aresample=async=1000:min_hard_comp=0.100:first_pts=0",
        "-c:a", "aac", "-b:a", ABR, "-ar", "48000", "-ac", "2",
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


# x11grab hands ffmpeg frames in real time, so the format probe always ends
# having seen one or two and says so -- every time any encoder starts, forever.
# The estimate it could not make is one we supply anyway with -framerate, so the
# line carries no information. Neither a smaller probe (-probesize 32
# -analyzeduration 0) nor the larger one the message itself suggests removes it.
# Dropping this single known line keeps the container logs worth reading, which
# matters more here than it sounds: real ffmpeg errors in this pod have twice
# been the actual cause of a stream failure.
BENIGN = ("not enough frames to estimate rate",)


def _relay_stderr(pipe):
    try:
        for line in iter(pipe.readline, b""):
            text = line.decode("utf-8", "replace").rstrip()
            if text and not any(b in text for b in BENIGN):
                print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


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
        self.bytes_in = 0
        self.bytes_out = 0

    def add(self, q):
        with self.lock:
            if self.init:
                self.clients.append(q)
            return self.init

    def drop(self, q):
        with self.lock:
            self.clients = [client for client in self.clients if client is not q]

    def reset(self):
        with self.lock:
            self.init = b""
            for q in self.clients:
                q.clear()
                q.append(None)  # End this response; the browser reconnects.
            self.clients.clear()

    def publish(self, chunk):
        with self.lock:
            for q in list(self.clients):
                if len(q) >= 60:
                    # Dropping bytes breaks MP4 framing and inter-frame video
                    # references. Reconnect with a fresh initialization instead.
                    q.clear()
                    q.append(None)
                    self.clients = [client for client in self.clients if client is not q]
                else:
                    q.append(chunk)
                    self.bytes_out += len(chunk)

    def run(self):
        """Keep one ffmpeg alive, ending viewers at each encoder boundary."""
        while True:
            self.reset()
            self.proc = None
            try:
                self.proc = subprocess.Popen(ffmpeg_argv(), stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, bufsize=1 << 20)
                threading.Thread(target=_relay_stderr, args=(self.proc.stderr,),
                                 daemon=True).start()
                self.started = time.time()
                self._pump(self.proc.stdout)
            except (OSError, ValueError) as exc:
                print("webstream: %s" % exc, file=sys.stderr, flush=True)
            finally:
                self.reset()
                if self.proc is not None:
                    if self.proc.poll() is None:
                        self.proc.kill()
                    self.proc.wait()
                    self.proc.stdout.close()
                self.restarts += 1
            time.sleep(1.0)

    def _pump(self, out):
        init = b""
        fragment = None
        while True:
            head = _read_exactly(out, 8)
            if head is None:
                return
            size, kind = struct.unpack(">I4s", head)
            if size == 1:
                ext = _read_exactly(out, 8)
                if ext is None:
                    return
                size = struct.unpack(">Q", ext)[0]
                head += ext
            if size < len(head) or size > 64 * 1024 * 1024:
                raise ValueError("invalid MP4 box size: %d" % size)
            body = _read_exactly(out, size - len(head))
            if body is None:
                return
            box = head + body
            self.bytes_in += len(box)
            if kind in (b"ftyp", b"moov"):
                init += box
                if kind == b"moov":
                    with self.lock:
                        self.init = init
            elif kind == b"moof":
                fragment = box
            elif kind == b"mdat" and fragment is not None:
                # Subscribe between complete fragments, never halfway through
                # the moof/mdat pair that describes one fragment's samples.
                self.publish(fragment + box)
                fragment = None


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
                "bytes_in": FAN.bytes_in,
                "bytes_out": FAN.bytes_out,
                "init_bytes": len(FAN.init),
                "uptime": round(time.time() - FAN.started, 1),
                "restarts": FAN.restarts,
                "encoder": {"preset": PRESET, "crf": CRF, "fps": FPS,
                            "size": SIZE, "audio": ABR},
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
        q = collections.deque()
        init = FAN.add(q)
        if not init:
            return self.send_error(503, "stream starting")
        try:
            self.connection.settimeout(10)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.chunk(init)
            idle = 0.0
            while True:
                with FAN.lock:
                    chunk = q.popleft() if q else b""
                if chunk == b"":
                    time.sleep(0.01)
                    idle += 0.01
                    # Nothing for this long means the encoder died rather than
                    # the screen being still -- fragments are emitted on a timer.
                    if idle > 30:
                        self.chunk(b"")
                        return
                    continue
                idle = 0.0
                if chunk is None:
                    self.chunk(b"")
                    return
                self.chunk(chunk)
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
