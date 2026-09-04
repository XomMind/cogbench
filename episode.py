#!/usr/bin/env python3
"""
Episode lifecycle for Cogbench: seeds, launch, run-end detection, results.

A benchmark needs runs that are comparable and terminations that are detected
without a human watching. Neither existed before this module.

## Profiles

Episodes run against an **isolated profile** (`~/Documents/Cogmind-bench` by
default), not the player's own. That buys three things at once: a `scorehistory.txt`
that starts empty so run detection is trivial, no interference with real saves,
and -- because the profile has no save file -- **launching necessarily starts a new
game** rather than resuming. That last point matters: `open -a Cogmind` on a
profile with a save silently resumes it, and the binary even contains a
"Resuming world seed: " string. `CMD_GAME_NEW` (Alt+F10) does reach the game but
puts up a prompt this harness has not learned to answer, so a save-free profile
is the reliable route to a fresh run.

## Seeds

`options.cfg` holds `worldSeed`. The in-game help is explicit: "Enter any
combination of numbers and/or letters to seed the game... Setting this only
affects future games." So the seed is set by editing that file **before launch**,
not through the UI. Cogmind rewrites the file on exit, so writing it while the
game runs would be silently discarded -- `set_seed` refuses to do that.

Verified deterministic: seed `COGBENCH1` twice produced identical map
fingerprints (`6e346a4203eb37c6`), and `COGBENCH2` produced a different one
(`1121a3191c1a26b0`).

## Run end

Three signals, cheapest first:

* `stats.integrity <= 0` -- in-memory, immediate.
* A new line in `user/scorehistory.txt` -- one per completed run, carrying score,
  deepest location, mode and the seed. Authoritative, and the file the benchmark
  actually scores from.
* A new file under `scores/`.

The binary contains "(no scoresheet for suicides below depth 9)", so a full
scoresheet is not guaranteed for every ending; `scorehistory.txt` is the reliable
one and is what `result()` reads.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import time

COGMIND_APP = "/Applications/Cogmind.app"
APP_CONTENTS = os.path.join(COGMIND_APP, "Contents")
SHARED = os.path.join(APP_CONTENTS, "SharedSupport")
GAME_DIR = os.path.join(SHARED, "prefix", "drive_c", "COGMIND (Beta 17.1)")
WINE = os.path.join(SHARED, "wine", "bin", "wine")

# The player's own profile, and the isolated one episodes use.
PLAYER_PROFILE = os.path.expanduser("~/Documents/Cogmind")
PROFILE = os.path.expanduser(os.environ.get("COGBENCH_PROFILE",
                                            "~/Documents/Cogmind-bench"))

def _p(profile=None):
    return os.path.expanduser(profile or PROFILE)

def options_path(profile=None):
    return os.path.join(_p(profile), "user", "options.cfg")

def scorehist_path(profile=None):
    return os.path.join(_p(profile), "user", "scorehistory.txt")

def scores_path(profile=None):
    return os.path.join(_p(profile), "scores")

OPTIONS = options_path()
SCOREHIST = scorehist_path()
SCORES_DIR = scores_path()
STATMIND = os.environ.get(
    "STATMIND_BIN", "/Users/heni/genAI/cogbench/StatMind/target/release/statmind"
)

# scorehistory.txt column header, from the binary:
#   Version Date Score Location P P U W Max Avg L 1 2 3 4 5 Maps Lore Gallery Achiev Mode Seed
SCOREHIST_FIELDS = [
    "version", "date", "score", "location",
    "p1", "p2", "u", "w", "max", "avg", "l",
    "c1", "c2", "c3", "c4", "c5",
    "maps", "lore", "gallery", "achiev", "mode", "seed",
]


# ---------------------------------------------------------------- process

def game_pids():
    """PIDs of the running Cogmind, empty if it is not up."""
    out = subprocess.run(["pgrep", "-f", "COGMIND.exe"],
                         capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if p.isdigit()]


def is_running():
    return bool(game_pids())


def quit_game(timeout=20):
    """Ask the game to exit, then insist. Returns True once it is gone."""
    subprocess.run(["pkill", "-f", "COGMIND.exe"], capture_output=True)
    subprocess.run(["pkill", "-f", "Sikarugir"], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running():
            return True
        time.sleep(0.5)
    subprocess.run(["pkill", "-9", "-f", "COGMIND.exe"], capture_output=True)
    time.sleep(1)
    return not is_running()


# ---------------------------------------------------------------- seeds

def read_seed(profile=None):
    path = options_path(profile)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        m = re.search(r'^worldSeed="([^"]*)"', f.read(), re.M)
    return m.group(1) if m else None


def set_seed(seed, profile=None):
    """Set the world seed for the *next* game.

    Refuses while the game is running: Cogmind rewrites options.cfg on exit, so
    the edit would be thrown away without any error to notice.
    """
    if is_running():
        raise RuntimeError(
            "Cogmind is running; it rewrites options.cfg on exit, so the seed "
            "would be discarded. Quit the game first."
        )
    path = options_path(profile)
    if not os.path.exists(path):
        raise RuntimeError(f"no options.cfg at {path}")
    seed = str(seed)
    if '"' in seed:
        raise ValueError("seed must not contain a double quote")

    backup = path + ".cogbench-backup"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)

    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if re.search(r'^worldSeed="[^"]*"', text, re.M):
        text = re.sub(r'^worldSeed="[^"]*"', f'worldSeed="{seed}"', text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f'\nworldSeed="{seed}"\n'
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    got = read_seed(profile)
    if got != seed:
        raise RuntimeError(f"seed did not take: wrote {seed!r}, read back {got!r}")
    return seed


def read_difficulty(profile=None):
    """0 = Rogue, 1 = Adventurer, 2 = Explorer. A benchmark must pin this."""
    path = options_path(profile)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        m = re.search(r"^difficultyMode=(\d+)", f.read(), re.M)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- statmind

def mcp(calls, timeout=300):
    """Run a list of (name, args) tool calls in one statmind session."""
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    for i, (name, args) in enumerate(calls):
        reqs.append({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                     "params": {"name": name, "arguments": args or {}}})
    payload = "\n".join(json.dumps(r) for r in reqs) + "\n"
    p = subprocess.run([STATMIND, "--mcp"], input=payload,
                       capture_output=True, text=True, timeout=timeout)
    out = {}
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        i = d.get("id")
        if not isinstance(i, int) or i < 10:
            continue
        key = calls[i - 10][0]
        if "error" in d:
            out[key] = {"_error": d["error"].get("message", "")}
        else:
            t = d["result"]["content"][0]["text"]
            try:
                out[key] = json.loads(t)
            except ValueError:
                out[key] = t
    return out


def wait_ready(timeout=120):
    """Wait until the game is up and LuigiAI has a populated map."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running():
            r = mcp([("luigi_raw", {})], timeout=90)
            v = r.get("luigi_raw")
            if isinstance(v, dict) and v.get("magic1_ok") and (v.get("map_width") or 0) > 0:
                return v
        time.sleep(2)
    return None


DEFAULT_OPTIONS = """playerName="cogbench"
worldSeed="{seed}"
uploadScores=0
newsUpdates=0
reportErrors=0
showIntro=0
showTutorial=0
difficultyMode={difficulty}
"""

# Automation needs every confirmation prompt off. Cogmind blocks movement behind
# several of them, and a blocked move looks identical to a wall from the outside:
# the descent bot deadlocked at one cell for six attempts because
# warnOnCaveinMove was defaulting to 1 and silently swallowing the keystroke.
DEFAULT_ADVANCED = """exposeKeybinds=1
jsonScoresheet=1
jsonStatDump=1
quickStart=1
disableAutoBackups=1
animateMapIntro=0
warnOnCaveinMove=0
warnOnMoveOutsideMapView=0
warnOnInventoryItemDropOnItem=0
ignoreAscendConfirmation=1
ignoreNeutralMeleeConfirmation=1
ignoreFlyingMeleeConfirmation=1
ignoreDestroyOnRemovalConfirmation=1
remindCorruptedInventoryOnExit=0
remindOnLeavingDroppedContainer=0
alwaysWarnAboutResearchers=0
autocommentHeavies=0
autocommentBehemoths=0
autocommentSentries=0
showAchievementMessages=0
autoOpenNewIntel=0
disableManualHackingHelp=1
noQuitDeleteSave=1
disableEscMenuAccessKeyboard=0
"""


def init_profile(profile=None, seed="0", difficulty=0, force=False):
    """Create (or reset) an isolated profile. No save file means a fresh run."""
    root = _p(profile)
    for sub in ("user", "scores", "screenshots"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    opts = os.path.join(root, "user", "options.cfg")
    if force or not os.path.exists(opts):
        with open(opts, "w", encoding="utf-8") as f:
            f.write(DEFAULT_OPTIONS.format(seed=seed, difficulty=difficulty))
    adv = os.path.join(root, "user", "advanced.cfg")
    if force or not os.path.exists(adv):
        with open(adv, "w", encoding="utf-8") as f:
            f.write(DEFAULT_ADVANCED)
    return root


def clear_saves(profile=None):
    """Remove save files so the next launch starts a new game. Only ever touches
    the isolated profile -- refuses to run against the player's own."""
    root = _p(profile)
    if os.path.realpath(root) == os.path.realpath(PLAYER_PROFILE):
        raise RuntimeError("refusing to delete saves from the player's own profile")
    removed = []
    d = os.path.join(root, "user")
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if name.startswith("save_") and name.endswith(".sav"):
            os.unlink(os.path.join(d, name))
            removed.append(name)
    return removed


def launch(seed=None, wait=True, profile=None, fresh=True):
    """Start the game against the isolated profile and wait until playable.

    Launched through the wrapper's wine directly rather than `open -a`, because
    that is the only way to point `-customFilePath` at our own profile. Two
    non-obvious requirements, both of which fail confusingly:

      * cwd must be the game directory, or it dies with
        "FATAL ERROR: init | Unable to open object data".
      * DYLD_FALLBACK_LIBRARY_PATH must include Contents/Frameworks, or
        wineserver cannot load libinotify.0.dylib. Note that wrapping the launch
        in `nohup` silently strips DYLD_* (SIP), reintroducing exactly this.
    """
    root = init_profile(profile, seed=str(seed) if seed is not None else "0")
    if is_running():
        quit_game()
    if seed is not None:
        set_seed(seed, profile=root)
    if fresh:
        clear_saves(root)

    win_profile = "Z:" + root.replace("/", "\\")
    env = dict(os.environ)
    env["WINEPREFIX"] = os.path.join(SHARED, "prefix")
    env.setdefault("WINEDEBUG", "-all")
    env["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join([
        os.path.join(APP_CONTENTS, "Frameworks"),
        os.path.join(SHARED, "wine", "lib"),
        "/usr/local/lib", "/usr/lib",
    ])
    log = open(os.environ.get("COGBENCH_WINE_LOG", "/tmp/cogbench-wine.log"), "wb")
    subprocess.Popen(
        [WINE, r"C:\COGMIND (Beta 17.1)\COGMIND.exe",
         "-luigiAi", f"-customFilePath:{win_profile}"],
        cwd=GAME_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return wait_ready() if wait else None


# ---------------------------------------------------------------- run end

def scorehist_lines(profile=None):
    """Completed-run rows of scorehistory.txt.

    The header is more than one line -- there is a title, a column header, a
    continuation header ("Slots---- Carried Security Level %...") and a dashes
    rule. Matching on known header prefixes missed two of them and let junk
    through as runs, so a row is instead accepted only if it *parses*: it needs a
    version token and an integer score. Validating beats pattern-matching here
    because the header can grow without warning.
    """
    path = scorehist_path(profile)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln.strip() or "----" in ln:
                continue
            rec = parse_scorehist_line(ln)
            if isinstance(rec.get("score"), int) and rec.get("version"):
                out.append(ln)
    return out


def parse_scorehist_line(line):
    """Parse one row of scorehistory.txt.

    The columns are whitespace-separated and *not* fixed-width in practice: a win
    row has a differently-shaped location ("Frmr" rather than "-5/UPP") and a
    different number of tokens, which shifts everything after it. Parsing purely
    left-to-right therefore mislabels the tail -- one real win row came out as
    `mode=81 seed=Rog`.

    So the ends are anchored independently: version/date/score/location from the
    left, and mode/seed from the right, where they always sit. The middle
    columns are positional and may still shift on unusual rows; `raw` is kept so
    a consumer can always re-read them.
    """
    parts = line.split()
    rec = {"raw": line}
    if len(parts) < 6:
        return rec
    off = 2 if parts[0].lower() == "beta" else 1   # "Beta 16", "Beta 11.1"
    rec["version"] = " ".join(parts[:off])
    # Right-anchored: Mode and Seed are always the final two columns.
    rec["mode"] = parts[-2]
    rec["seed"] = parts[-1]

    mid = parts[off:-2]
    middle = SCOREHIST_FIELDS[1:-2]          # date .. achiev, 19 columns
    extra = len(mid) - len(middle)
    if extra > 0:
        # Locations are sometimes multi-word ("Frmr TC4", "Tau Ceti"), which
        # shifts everything after them. Absorb the surplus tokens into it.
        loc_at = middle.index("location")
        head = mid[:loc_at]
        loc = " ".join(mid[loc_at:loc_at + 1 + extra])
        tail = mid[loc_at + 1 + extra:]
        mid = head + [loc] + tail
    # Never let the positional pass clobber the right-anchored fields.
    for name, val in zip(middle, mid):
        rec[name] = val
    try:
        rec["score"] = int(rec.get("score", ""))
    except (TypeError, ValueError):
        rec["score"] = None   # not a data row, or an unexpected column layout
    return rec


def read_scoresheet(path):
    """Parse a JSON scoresheet.

    `jsonScoresheet=1` makes Cogmind write both a .txt and a .json per run. The
    JSON is the scoring oracle and is far easier than the protobuf -- but note its
    keys are **camelCase** (`totalScore`, `runResult`), not the snake_case of
    scoresheet.proto, so proto field names do not transfer.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        d = json.load(f)

    def dig(*keys, default=None):
        o = d
        for k in keys:
            if isinstance(o, dict) and k in o:
                o = o[k]
            else:
                return default
        return o

    route = dig("route", "entries", default=[]) or []
    depths = [e.get("location", {}).get("depth") for e in route]
    depths = [x for x in depths if isinstance(x, int)]
    return {
        "file": os.path.basename(path),
        "version": dig("header", "version"),
        "build": dig("header", "build"),
        "player": dig("header", "playerName"),
        # e.g. "Destroyed by H-55 Commando with Assault Rifle"
        "run_result": dig("header", "runResult"),
        "win": bool(dig("header", "win", default=False)),
        "score": dig("performance", "totalScore"),
        "maps_visited": len(route),
        "route": [(e.get("location", {}).get("depth"),
                   e.get("location", {}).get("map")) for e in route],
        # Cogmind depths are negative and you *ascend* toward the surface, so
        # progress is the MAXIMUM (least negative) depth reached -- calling the
        # minimum "deepest" reads as progress and is exactly backwards.
        "start_depth": min(depths) if depths else None,
        "best_depth": max(depths) if depths else None,
        "bonus": dig("bonus", default={}),
        "best_states": dig("bestStates", default={}),
        "last_messages": (dig("lastMessages", "messages", default=[]) or [])[-5:],
    }


def latest_scoresheet(profile=None):
    d = scores_path(profile)
    if not os.path.isdir(d):
        return None
    js = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    return read_scoresheet(os.path.join(d, js[-1])) if js else None


def scores_dir_files(profile=None):
    path = scores_path(profile)
    if not os.path.isdir(path):
        return set()
    return set(os.listdir(path))


class RunWatcher:
    """Snapshot the run-end signals, then detect a new terminated run."""

    def __init__(self, profile=None):
        self.profile = profile
        self.lines0 = scorehist_lines(profile)
        self.files0 = scores_dir_files(profile)
        self.n0 = len(self.lines0)

    def poll(self):
        """Return a result dict once the run has ended, else None."""
        lines = scorehist_lines(self.profile)
        if len(lines) > self.n0:
            new = lines[self.n0:]
            rec = parse_scorehist_line(new[-1])
            rec["new_score_files"] = sorted(scores_dir_files(self.profile) - self.files0)
            rec["source"] = "scorehistory"
            return rec
        files = scores_dir_files(self.profile) - self.files0
        if files:
            return {"source": "scores_dir", "new_score_files": sorted(files)}
        if not is_running():
            return {"source": "process_exit",
                    "note": "the game exited without writing a score row; "
                            "quit before dying, or an ending with no scoresheet"}
        return None

    def wait(self, timeout=600, interval=2.0, on_tick=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.poll()
            if r is not None:
                return r
            if on_tick:
                on_tick()
            time.sleep(interval)
        return None


def alive_check(integrity_hint=None, matter_hint=None):
    """In-memory liveness. `integrity <= 0` is the immediate death signal, but it
    needs the stat block, which is heap-resident and located by value."""
    calls = [("player", {})]
    if integrity_hint is not None and matter_hint is not None:
        calls.append(("find_stats", {"integrity": integrity_hint, "matter": matter_hint}))
    r = mcp(calls)
    pl = r.get("player") or {}
    out = {"player_plausible": bool(pl.get("plausible")),
           "player": {k: pl.get(k) for k in ("x", "y", "handle", "entity_name")}}
    st = r.get("find_stats")
    if isinstance(st, dict) and st.get("stats"):
        s = st["stats"][0]
        out["stats"] = s
        out["dead"] = s["integrity"] <= 0
    return out


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="Cogbench episode lifecycle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="game state, seed, difficulty, last run")
    p = sub.add_parser("seed", help="set the seed for the next game")
    p.add_argument("value")
    p = sub.add_parser("launch", help="start a fresh seeded run in the isolated profile")
    p.add_argument("--seed")
    p.add_argument("--resume", action="store_true",
                   help="do not clear saves (resume instead of starting fresh)")
    sub.add_parser("fingerprint", help="hash the current map, to check determinism")
    p = sub.add_parser("init", help="create/reset the isolated profile")
    p.add_argument("--seed", default="0")
    p.add_argument("--difficulty", type=int, default=0)
    p.add_argument("--force", action="store_true")
    sub.add_parser("quit", help="stop the game")
    p = sub.add_parser("watch", help="wait for the current run to end")
    p.add_argument("--timeout", type=int, default=600)
    sub.add_parser("history", help="parse scorehistory.txt")
    sub.add_parser("scoresheet", help="parse the newest JSON scoresheet")
    a = ap.parse_args()

    if a.cmd == "init":
        root = init_profile(seed=a.seed, difficulty=a.difficulty, force=a.force)
        print(f"profile {root}  seed={read_seed()!r}  difficulty={read_difficulty()}")
        return 0

    if a.cmd == "fingerprint":
        import hashlib
        r = mcp([("luigi_raw", {}), ("player", {}), ("get_map", {})])
        m = r.get("get_map")
        if not isinstance(m, dict) or "cells" not in m:
            print("no map:", str(m)[:160]); return 1
        lr = r.get("luigi_raw") or {}; pl = r.get("player") or {}
        cells = sorted((c["x"], c["y"], c["cell_id"]) for c in m["cells"])
        print(json.dumps({
            "seed": read_seed(), "w": m["width"], "h": m["height"],
            "depth": lr.get("location_depth"), "map_type": lr.get("location_map"),
            "player": [pl.get("x"), pl.get("y")],
            "fingerprint": hashlib.sha256(repr(cells).encode()).hexdigest()[:16],
        }))
        return 0

    if a.cmd == "status":
        print(f"profile        {PROFILE}")
        print(f"running        {is_running()}  pids={game_pids()}")
        print(f"seed           {read_seed()!r}")
        d = read_difficulty()
        print(f"difficulty     {d} ({ {0:'Rogue',1:'Adventurer',2:'Explorer'}.get(d,'?') })")
        print(f"score rows     {len(scorehist_lines())}")
        print(f"scores/ files  {len(scores_dir_files())}")
        if is_running():
            v = mcp([("luigi_raw", {})]).get("luigi_raw", {})
            if isinstance(v, dict) and "_error" not in v:
                print(f"in-game        {v.get('map_width')}x{v.get('map_height')} "
                      f"depth={v.get('location_depth')} map_type={v.get('location_map')}")
        rows = scorehist_lines()
        if rows:
            r = parse_scorehist_line(rows[-1])
            print(f"last run       score={r.get('score')} loc={r.get('location')} "
                  f"mode={r.get('mode')} seed={r.get('seed')}")
        return 0

    if a.cmd == "seed":
        print("seed set to", set_seed(a.value))
        return 0

    if a.cmd == "quit":
        print("stopped" if quit_game() else "could not stop the game")
        return 0

    if a.cmd == "launch":
        v = launch(seed=a.seed, fresh=not a.resume)
        if v is None:
            print("launched but LuigiAI never populated a map "
                  "(still at the menu or the opening dialogue?)")
            return 1
        print(f"ready: {v['map_width']}x{v['map_height']} depth={v['location_depth']} "
              f"seed={read_seed()!r}")
        return 0

    if a.cmd == "watch":
        w = RunWatcher()
        print(f"watching (baseline {w.n0} score rows, {len(w.files0)} score files)...")
        r = w.wait(timeout=a.timeout,
                   on_tick=lambda: print(".", end="", flush=True))
        print()
        print(json.dumps(r, indent=1) if r else "timed out; run still going")
        return 0 if r else 1

    if a.cmd == "scoresheet":
        r = latest_scoresheet()
        if not r:
            print("no scoresheet in", scores_path()); return 1
        print(json.dumps(r, indent=1))
        return 0

    if a.cmd == "history":
        rows = scorehist_lines()
        print(f"{len(rows)} completed runs")
        for line in rows[-10:]:
            r = parse_scorehist_line(line)
            print(f"  {r.get('version','?'):8} score={str(r.get('score')):>7} "
                  f"loc={str(r.get('location')):9} mode={str(r.get('mode')):4} "
                  f"seed={r.get('seed')}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
