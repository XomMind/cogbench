#!/usr/bin/env python3
"""
Live state for the OBS overlay.

The agent writes one JSON file per decision and a static page polls it. That is
deliberately the dumbest thing that works: no sockets, no server inside the
agent, nothing that can wedge the run if the browser goes away. OBS points a
Browser Source at the server below and never talks to the agent at all.

Writes are atomic (tmp + rename) because the page polls twice a second and will
absolutely catch a half-written file otherwise -- which shows up as the overlay
blanking at random and is deeply confusing to debug live on stream.

    ./stream.py serve                 # then add a Browser Source at the URL
    ./agent.py --model ... --stream   # anything with --stream feeds it
"""

import argparse
import http.server
import json
import os
import socketserver
import urllib.parse
import urllib.error
import urllib.request
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Static files ship with the code; the state file is written at runtime by
# whoever is playing. In the container those are different filesystems -- the
# page is baked into a read-only /opt, the state lives on the PVC -- so they
# are separate knobs and the handler stitches them back together.
DIR = os.path.join(HERE, "stream")
STATE_DIR = os.environ.get("COGBENCH_STREAM_DIR", DIR)
STATE = os.path.join(STATE_DIR, "state.json")
PARAMS = os.path.join(STATE_DIR, "params.json")
PORT = 8777


def publish(state, path=STATE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["t"] = time.time()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


FRAME = os.environ.get("COGBENCH_FRAME", "/run/frames/frame.jpg")
# One line of commentary, on demand. Kept out of the agent deliberately: the
# agent's loop is the benchmark, and a button that makes it stop and write
# jokes would be measuring something else. This asks the same endpoint the same
# question a viewer would, using the state the agent already published.
QUIP = os.path.join(STATE_DIR, "quip.json")

# The live knobs. Anything here can be changed mid-run from control.html.
DEFAULT_PARAMS = {
    "cam": False,             # reserve the webcam square
    "chat_window_min": 10,    # how far back the model is shown chat
    "chat_limit": 20,         # and at most how many messages
    "chat_on": True,
    "model": "",              # empty = whatever the agent was started with
}

# The run lifecycle lives in the game container -- that is where the agent
# process has to run -- and is proxied from here so the control page has a
# single origin. Containers in a pod share a network namespace, so loopback
# reaches it; it is not routable from outside the pod.
RUNNER = os.environ.get("COGBENCH_RUNNER", "http://127.0.0.1:8099")

# Where to ask what models exist. Same endpoint the agent plays through.
ENDPOINT = os.environ.get("COGBENCH_URL", "")
API_KEY = os.environ.get("COGBENCH_API_KEY", "")
_models = {"at": 0.0, "ids": []}


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # MJPEG, fanned out from the single frame file ffmpeg keeps rewriting.
        # Serving the file rather than piping ffmpeg's stdout means any number
        # of clients, and OBS reconnecting mid-stream costs nothing. ffmpeg
        # writes it with -atomic_writing, so a reader never catches a half file.
        if self.path.startswith("/video.mjpg"):
            return self.mjpeg()
        if self.path.split("?")[0] == "/state.json":
            return self.sendfile(STATE, "application/json")
        if self.path.split("?")[0] in ("/params", "/scene"):
            return self.params()
        if self.path.split("?")[0] == "/models":
            return self.models()
        if self.path.startswith("/run"):
            return self.runner()
        if self.path.split("?")[0] == "/quip.json":
            return self.sendfile(QUIP, "application/json")
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] in ("/params", "/scene"):
            return self.params()
        if self.path.startswith("/run"):
            return self.runner()
        if self.path.split("?")[0] == "/say":
            return self.say()
        self.send_error(404)

    def runner(self):
        """Pass /run/* through to the runner in the game container.

        A proxy rather than a redirect because the runner binds loopback: the
        browser cannot reach it directly, and that is deliberate. Errors come
        back as JSON rather than an HTTP error so the control page can show
        "runner unreachable" in place of a status instead of going blank.
        """
        body = None
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
        try:
            req = urllib.request.Request(
                RUNNER + self.path, data=body, method=self.command,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            out, code = e.read(), e.code
        except Exception as e:
            out = json.dumps({"error": "runner unreachable: %s: %s"
                              % (type(e).__name__, e)}).encode()
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def say(self):
        """Ask the model for one sentence about what is happening.

        POST /say speaks, POST /say?clear=1 wipes it. The result goes in its
        own small file rather than into state.json, because state.json belongs
        to the agent and is rewritten every decision -- anything this wrote
        there would last about a second.
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "clear" in q:
            self._write_quip({"text": "", "t": time.time()})
            return self.jsonout({"text": ""})
        try:
            with open(STATE) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return self.jsonout({"error": "no run state yet"}, 409)
        run, perf = st.get("run") or {}, st.get("perf") or {}
        recent = ", ".join("%s -> %s" % (d.get("action"), d.get("result"))
                           for d in (st.get("recent") or [])[:4])
        prompt = (
            "You are the AI playing Cogmind on a live stream. In ONE short "
            "sentence, in character and with some personality, say something "
            "about how it is going right now. No quotes, no preamble.\n\n"
            "Depth %s, turn %s, decision %s. Core %s. Recent: %s"
            % (run.get("depth"), run.get("turn"), run.get("decisions"),
               (st.get("res") or {}).get("core_integrity"), recent or "nothing yet"))
        model = perf.get("model") or ""
        if not ENDPOINT or not model:
            return self.jsonout({"error": "no endpoint or model to ask"}, 409)
        base = ENDPOINT.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Short and cheap: this runs on the same server the agent is
            # playing through, and a long generation here is latency stolen
            # from the run itself.
            "max_tokens": 60, "temperature": 0.9,
        }).encode()
        try:
            req = urllib.request.Request(
                base + "/v1/chat/completions", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         **({"Authorization": "Bearer " + API_KEY} if API_KEY else {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
            text = out["choices"][0]["message"]["content"].strip().strip('"')
        except Exception as e:
            return self.jsonout({"error": "%s: %s" % (type(e).__name__, e)}, 502)
        text = " ".join(text.split())[:240]
        self._write_quip({"text": text, "t": time.time(), "model": model})
        return self.jsonout({"text": text})

    def _write_quip(self, obj):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = QUIP + ".tmp"
            with open(tmp, "w") as f:
                json.dump(obj, f)
            os.replace(tmp, QUIP)
        except OSError:
            pass

    def jsonout(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def models(self):
        """The endpoint's model list, proxied.

        Proxied rather than fetched by the page directly so the API key stays
        server-side -- a browser source on a streaming machine is the last
        place to put a credential. Cached for a few seconds because the control
        page polls, with ?refresh=1 to force a re-ask when someone has just
        loaded a new model.
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        fresh = "refresh" in q
        if ENDPOINT and (fresh or time.time() - _models["at"] > 10):
            try:
                base = ENDPOINT.rstrip("/")
                if base.endswith("/v1"):
                    base = base[:-3]
                req = urllib.request.Request(
                    base + "/v1/models",
                    headers={"Authorization": "Bearer " + API_KEY} if API_KEY else {})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.load(r)
                _models["ids"] = [m["id"] for m in data.get("data", [])]
                _models["at"] = time.time()
            except Exception as e:
                _models["err"] = "%s: %s" % (type(e).__name__, e)
        body = json.dumps({"models": _models["ids"],
                           "error": _models.get("err") if not _models["ids"] else None,
                           "age": round(time.time() - _models["at"], 1)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def params(self):
        """Live knobs, shared by the overlay and whoever is playing.

        Everything tunable mid-stream lives in one small JSON on the PVC:
        the scene layout, and how much Twitch chat the model is shown. Both
        sides poll it, so a change takes effect on the next decision without
        restarting the agent or reloading a browser source.

        `cam` is one of these knobs rather than a URL flag so that a SINGLE
        Browser Source can be added to several OBS scenes and re-lay-out in
        place -- two sources pointing at ?cam=1 and ?cam=0 would each pull
        their own copy of the video and reload on every switch.
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        cur = dict(DEFAULT_PARAMS)
        try:
            with open(PARAMS) as f:
                cur.update(json.load(f))
        except (OSError, ValueError):
            pass
        if q:
            for k, v in q.items():
                if k not in DEFAULT_PARAMS:
                    continue
                raw = v[0]
                if isinstance(DEFAULT_PARAMS[k], bool):
                    cur[k] = raw not in ("0", "false", "off", "")
                elif isinstance(DEFAULT_PARAMS[k], (int, float)):
                    try:
                        cur[k] = type(DEFAULT_PARAMS[k])(raw)
                    except ValueError:
                        pass
                else:
                    cur[k] = raw
            # Clamp rather than trust: these come from a web page and end up
            # shaping a prompt.
            cur["chat_window_min"] = max(0, min(120, cur["chat_window_min"]))
            cur["chat_limit"] = max(0, min(200, cur["chat_limit"]))
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                tmp = PARAMS + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(cur, f)
                os.replace(tmp, PARAMS)
            except OSError:
                pass
        body = json.dumps(cur).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def sendfile(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def mjpeg(self, fps=15):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                try:
                    st = os.stat(FRAME)
                    if (st.st_mtime, st.st_size) != last:
                        with open(FRAME, "rb") as f:
                            jpg = f.read()
                        last = (st.st_mtime, st.st_size)
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: %d\r\n\r\n" % len(jpg))
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")
                except FileNotFoundError:
                    pass
                time.sleep(1.0 / fps)
        except (BrokenPipeError, ConnectionResetError):
            pass          # OBS closed the source; not an error
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)

    def translate_path(self, path):
        """Serve from the state directory first, then the baked-in copy.

        The image ships a known-good overlay; dropping a file of the same name
        on the PVC overrides it. That makes a CSS fix a `kubectl cp` instead of
        a rebuild and a rollout, which matters when the thing being tuned is a
        layout you can only judge by looking at it.
        """
        baked = super().translate_path(path)
        rel = os.path.relpath(baked, DIR)
        if not rel.startswith(".."):
            override = os.path.join(STATE_DIR, rel)
            if os.path.isfile(override):
                return override
        return baked

    def end_headers(self):
        # The overlay polls the same URL forever; without this OBS's cache
        # happily serves the first frame for the rest of the stream.
        self.send_header("Cache-Control", "no-store")
        # The watch page is served from the video port and reads state.json
        # from this one, which the browser treats as cross-origin and blocks
        # without this header. Everything here is already readable by anyone
        # who can reach the port, so allowing any origin gives nothing away.
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *a):
        pass                      # the console belongs to the agent


def serve(port=PORT):
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(STATE):
        publish({"status": "waiting for the agent"})
    # Threaded: an MJPEG client holds its connection open forever, and a
    # single-threaded server would then never answer state.json again.
    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    # 0.0.0.0 because OBS is on another machine; the only thing in front of
    # this is the cluster's NodePort.
    with Server(("0.0.0.0", port), Handler) as srv:
        print("overlay:  http://0.0.0.0:%d/overlay.html" % port)
        print("video:    http://0.0.0.0:%d/video.mjpg" % port)
        print("state:    %s" % STATE)
        print("control:  http://0.0.0.0:%d/control.html" % port)
        print("runner:   %s (proxied at /run/status)" % RUNNER)
        print("OBS: Browser Source -> the overlay URL at 1280x720, and "
              "uncheck 'Shutdown source when not visible'")
        srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["serve"])
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    try:
        serve(a.port)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    sys.exit(main() or 0)
