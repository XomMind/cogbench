#!/usr/bin/env python3
"""
Run lifecycle for the agent: start, stop, restart, and revive when it dies.

This exists because the agent is a process, not a service. Until now a run was
started by hand with `kubectl exec ... setsid nohup python3 agent.py &`, which
means the only way to restart after a crash was to be at a terminal with
cluster credentials -- no good mid-stream, and no good at all if the thing died
while nobody was watching.

It runs in the game container rather than alongside the overlay because that is
where the agent has to live: it needs the same filesystem as Cogmind, the same
X display, and the same wineserver. The overlay's stream.py proxies /run/* here
over the pod's loopback, so the control page reaches it without this ever being
exposed outside the pod -- it binds 127.0.0.1 and has no authentication, which
is only safe because of that.

    ./runner.py serve --port 8099
"""

import argparse
import http.server
import json
import math
import os
import signal
import socketserver
import random
import string
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("COGBENCH_STREAM_DIR", os.path.join(HERE, "stream"))
# The last run's arguments, so a restart after a pod bounce replays what was
# actually playing rather than a default nobody chose.
SPEC = os.path.join(STATE_DIR, "run-spec.json")
RESULT = os.path.join(STATE_DIR, "run-result.json")
LOG = os.environ.get("COGBENCH_AGENT_LOG", "/data/agent.log")
AGENT = os.path.join(HERE, "agent.py")
STATMIND = os.environ.get("COGBENCH_STATMIND", "/usr/local/bin/statmind")
PORT = 8099

# What a start request may set. Anything not here cannot be injected into the
# command line from a web page -- the runner builds argv as a list, never a
# shell string, and unknown keys are dropped rather than passed through.
DEFAULT_SPEC = {
    "model": "",
    "decisions": 600,
    "twitch": "",
    "policy": "model",
    "temperature": 0.7,
    "supervise": True,  # bring it back by itself when it dies
    "loop": False,  # and start a NEW episode when one is played out
    "seed": "",  # per-episode seed; empty = a fresh one each time
    # Set by loop mode just before it ends the container, and read by the next
    # container's entrypoint. Persisting the intent is the only way it can
    # survive the restart that IS the episode boundary.
    "autostart": False,
}
POLICIES = ("model", "heuristic", "scripts")


def load_spec():
    spec = dict(DEFAULT_SPEC)
    try:
        with open(SPEC) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            spec = coerce(spec, saved)
    except (OSError, ValueError):
        pass
    return spec


def save_spec(spec):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SPEC + ".tmp"
        with open(tmp, "w") as f:
            json.dump(spec, f)
        os.replace(tmp, SPEC)
    except OSError:
        pass


def coerce(spec, raw):
    """Fold web-page values into a spec, clamped and typed.

    Everything here arrives from a browser and ends up as process arguments,
    so each field is converted to the type of its default and range-checked.
    A bad value is ignored rather than rejected: half a form getting through
    is better than a start request failing mid-stream over a stray character.
    """
    out = dict(spec)
    for k, v in raw.items():
        if k not in DEFAULT_SPEC:
            continue
        if isinstance(DEFAULT_SPEC[k], bool):
            out[k] = str(v).lower() not in ("0", "false", "off", "")
        elif isinstance(DEFAULT_SPEC[k], int):
            try:
                out[k] = int(v)
            except (TypeError, ValueError, OverflowError):
                pass
        elif isinstance(DEFAULT_SPEC[k], float):
            try:
                value = float(v)
                if math.isfinite(value):
                    out[k] = value
            except (TypeError, ValueError, OverflowError):
                pass
        else:
            out[k] = str(v)
    out["decisions"] = max(1, min(100000, out["decisions"]))
    out["temperature"] = max(0.0, min(2.0, out["temperature"]))
    if out["policy"] not in POLICIES:
        out["policy"] = "model"
    out["twitch"] = out["twitch"].lstrip("#").strip()
    return out


NEXT_SEED = os.environ.get("COGBENCH_NEXT_SEED", "/data/next-seed")


def new_episode(seed):
    """Put Cogmind back at the start of a fresh run, the container's way.

    episode.py's own launch() is for a workstation: it quits the game and
    starts another one itself, against a hardcoded /Applications path. In here
    neither half applies. The entrypoint owns the game process -- it seeds the
    profile, starts Cogmind, and exits when Cogmind does -- so the container is
    already a one-episode unit, and the way to get another episode is to let it
    end and have kubelet bring it back.

    So this writes the seed the next container should use, arms the autostart,
    and quits the game. The runner dies along with everything else a moment
    later, which is why the intent has to be on disk before the quit and not
    held in this process.
    """
    try:
        with open(NEXT_SEED, "w") as f:
            f.write(seed)
    except OSError as e:
        return False, "cannot write %s: %s" % (NEXT_SEED, e)
    spec = load_spec()
    spec["autostart"] = True
    save_spec(spec)
    # End the container, not just the game, and do it definitively.
    #
    # Two gentler things were tried and neither ends the episode. pkill on
    # COGMIND.exe alone leaves the entrypoint's watch loop none the wiser, so
    # the pod stays up with no game in it and every later run dies on "statmind
    # exited". SIGTERM to PID 1 runs the entrypoint's cleanup trap, but a trap
    # handler returns into the script rather than ending it, and the kills in
    # it miss: game_pid is the wine launcher, not COGMIND.exe, which survives
    # it. Both were observed leaving the pod alive and unplayable.
    #
    # So: stop the game properly, then SIGKILL PID 1. The container exits,
    # kubelet restarts it, and the new entrypoint seeds a fresh profile and
    # starts a genuinely new run. Nothing is lost to the abruptness -- the
    # episode is over by definition, and every file this harness writes is
    # written atomically.
    subprocess.run(["pkill", "-f", "COGMIND.exe"], capture_output=True)
    time.sleep(2)
    try:
        os.kill(1, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as e:
        return False, "cannot end the container: %s" % e
    return True, "container restarting for a new episode"


def some_seed():
    return "".join(
        random.choice(string.ascii_uppercase + string.digits) for _ in range(8)
    )


def argv_for(spec):
    argv = [
        sys.executable,
        AGENT,
        "--statmind",
        STATMIND,
        "--policy",
        spec["policy"],
        "--decisions",
        str(spec["decisions"]),
        "--temperature",
        str(spec["temperature"]),
        "--stream",
        "--out",
        RESULT,
    ]
    if spec["model"]:
        argv += ["--model", spec["model"]]
    if spec["twitch"]:
        argv += ["--twitch", spec["twitch"]]
    return argv


class Runner:
    """Owns at most one agent process.

    Every transition takes the lock, because the supervisor thread and an HTTP
    request can both decide to act on the same dead process -- without it, a
    crash during a manual restart starts the agent twice, and two agents on one
    Cogmind is not a recoverable state.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.spec = load_spec()
        self.started = 0.0
        self.stopping = False  # a stop we asked for, not a crash
        self.last = None  # how the previous run ended
        self.revivals = 0

    # -- state ------------------------------------------------------------
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        with self.lock:
            st = {
                "running": self.alive(),
                "pid": self.proc.pid if self.alive() else None,
                "uptime": round(time.time() - self.started, 1) if self.alive() else 0,
                "spec": dict(self.spec),
                "last": self.last,
                "revivals": self.revivals,
                "log": LOG,
            }
        st["decisions"] = self._decisions()
        return st

    def _decisions(self):
        """Progress, read from the stream state rather than counted here.

        The agent already publishes it every decision; parsing the log for it
        would be a second source of truth that disagrees the moment the log
        rotates or a line is half-written.
        """
        try:
            with open(os.path.join(STATE_DIR, "state.json")) as f:
                return (json.load(f).get("run") or {}).get("decisions")
        except (OSError, ValueError):
            return None

    def tail(self, n=40):
        try:
            with open(LOG, "rb") as f:
                try:
                    f.seek(-64 * 1024, os.SEEK_END)
                    f.readline()  # drop the partial first line
                except OSError:
                    pass  # log shorter than the window
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        return lines[-max(1, min(400, n)) :]

    # -- transitions ------------------------------------------------------
    def start(self, spec=None):
        with self.lock:
            if self.alive():
                return False, "already running"
            if spec:
                self.spec = spec
                save_spec(self.spec)
            # Truncate rather than append: the log is what the control page
            # shows when a run dies, and the reason is useless if it is buried
            # under the previous three runs.
            try:
                os.makedirs(os.path.dirname(LOG), exist_ok=True)
                os.makedirs(STATE_DIR, exist_ok=True)
                if os.path.exists(RESULT):
                    os.unlink(RESULT)
                log = open(LOG, "wb", buffering=0)
            except OSError as e:
                return False, "cannot open %s: %s" % (LOG, e)
            self.stopping = False
            try:
                self.proc = subprocess.Popen(
                    argv_for(self.spec),
                    cwd=HERE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    # Its own process group, so stopping the agent kills any
                    # child it spawned and never reaches this runner.
                    start_new_session=True,
                )
            except OSError as e:
                log.close()
                return False, str(e)
            finally:
                try:
                    log.close()  # the child holds its own descriptor
                except OSError:
                    pass
            self.started = time.time()
            # The arming is spent. Loop mode re-arms on its way out of each
            # episode, so clearing it here only affects restarts nobody asked
            # for -- a node drain, an eviction -- which should not start
            # playing on their own.
            if self.spec.get("autostart"):
                self.spec["autostart"] = False
                save_spec(self.spec)
            return True, "started pid %d" % self.proc.pid

    def stop(self, timeout=10.0):
        # Keep transitions serialized through process exit. In particular a
        # pending supervisor revival must observe even a stop of a dead agent.
        with self.lock:
            self.stopping = True
            if self.spec.get("autostart"):
                self.spec["autostart"] = False
                save_spec(self.spec)
            if not self.alive():
                return False, "not running; automatic revival cancelled"
            proc = self.proc
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            return True, "stopped"

    def restart(self, spec=None):
        with self.lock:
            self.stop()
            return self.start(spec)

    # -- supervision ------------------------------------------------------
    def reap(self):
        """Notice a finished run, record why, and revive it if asked.

        Called on a timer instead of waiting on the process so that a run that
        ends while the control page is closed is still recorded, and so the
        revive decision is made in one place rather than racing the handler.
        """
        with self.lock:
            if self.proc is None or self.proc.poll() is None:
                return
            code = self.proc.returncode
            try:
                with open(RESULT) as f:
                    result = json.load(f)
                if not isinstance(result, dict):
                    result = {}
            except (OSError, ValueError):
                result = {}
            self.last = {
                "result": result,
                "code": code,
                # A clean finish is the agent reaching --decisions; anything
                # else is a crash or a stop, and the distinction is what the
                # control page colours.
                "why": (
                    "stopped"
                    if self.stopping
                    else (
                        result.get("status", "finished")
                        if code == 0
                        else (
                            "killed (signal %d)" % -code
                            if code < 0
                            else "crashed (exit %d)" % code
                        )
                    )
                ),
                "at": time.time(),
                "ran": round(time.time() - self.started, 1),
                "decisions": self._decisions(),
                "tail": self.tail(12),
            }
            self.proc = None
            asked = self.stopping
            spec = dict(self.spec)
            self.stopping = False
        # A failure resumes the same episode. Only an explicit ended result
        # may discard it; exit zero also covers an exhausted decision budget.
        revive = spec.get("supervise") and not asked and code != 0
        fresh = (
            spec.get("loop")
            and not asked
            and code == 0
            and result.get("status") == "ended"
        )
        if not (revive or fresh):
            return
        if fresh:
            with self.lock:
                if self.stopping or self.proc is not None or not self.spec.get("loop"):
                    return
                ok, out = new_episode(spec.get("seed") or some_seed())
                self.last["episode"] = "new episode" if ok else "relaunch failed"
                if not ok:
                    self.last["tail"] = (
                        self.last.get("tail") or []
                    ) + out.splitlines()[-6:]
            if not ok:
                return
            # On success this container is on its way out; the next one's
            # entrypoint starts the agent from the armed spec. Returning here
            # rather than starting avoids racing our own shutdown.
            return
        # A crash loop must not become a hot loop: Cogmind takes a moment to be
        # ready again, and an instant respawn just crashes faster.
        time.sleep(5)
        with self.lock:
            if self.stopping or self.proc is not None or not self.spec.get("supervise"):
                return
            ok, _ = self.start()
            if ok:
                self.revivals += 1


RUN = Runner()


def supervisor():
    while True:
        try:
            RUN.reap()
        except Exception:
            pass  # a supervisor that dies is worse
        time.sleep(2)


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def args(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = {k: v[0] for k, v in q.items()}
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                if isinstance(body, dict):
                    raw.update({k: v for k, v in body.items()})
            except ValueError:
                pass
        return raw

    def route(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path.startswith("/run"):
            path = path[4:] or "/"
        raw = self.args()
        if path in ("/", "/status"):
            return self.reply(RUN.status())
        if path == "/log":
            return self.reply({"lines": RUN.tail(int(raw.get("n", 40) or 40))})
        if path == "/start":
            ok, msg = RUN.start(coerce(RUN.spec, raw))
            return self.reply(dict(RUN.status(), ok=ok, msg=msg))
        if path == "/stop":
            ok, msg = RUN.stop()
            return self.reply(dict(RUN.status(), ok=ok, msg=msg))
        if path == "/restart":
            ok, msg = RUN.restart(coerce(RUN.spec, raw))
            return self.reply(dict(RUN.status(), ok=ok, msg=msg))
        if path == "/spec":
            spec = coerce(RUN.spec, raw)
            with RUN.lock:
                RUN.spec = spec
            save_spec(spec)
            return self.reply(RUN.status())
        self.reply({"error": "no such endpoint"}, 404)

    do_GET = do_POST = route

    def log_message(self, *a):
        pass


def serve(port=PORT):
    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    threading.Thread(target=supervisor, daemon=True).start()
    # Loopback only. Containers in a pod share a network namespace, so the
    # overlay reaches this without it being routable from anywhere else --
    # which is the whole reason it needs no auth.
    with Server(("127.0.0.1", port), Handler) as srv:
        print("runner: http://127.0.0.1:%d/run/status" % port)
        srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=["serve"])
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    try:
        serve(a.port)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    sys.exit(main() or 0)
