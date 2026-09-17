#!/usr/bin/env python3
"""
Read Cogmind's own mid-run stat dumps.

Why this is the primary observation channel
-------------------------------------------
Cogmind can serialise the run in progress on demand -- the manual calls it a
stat dump, and `Alt-Shift-S` (CMD_BS_DEFAULT_OUTPUT_DUMP) drives it. The shim
calls the same writer directly (see StatMind/SDL-1.2/src/statmind_scoresheet.h),
so a dump costs one mailbox round trip and lands as `.txt` plus `.json` under
the profile's `dumps/`.

The result carries things this harness could not otherwise get:

* **Resource maxima.** `energy: 100/250` -- the maximum is *derived* from
  attached parts, so it exists nowhere in memory to scan for. An earlier
  structural scan for the stat block failed precisely because it looked for the
  maxima.
* **The loadout by slot and by name**, inventory included. The descent bot had
  no model of parts at all and died with almost nothing attached.
* **The known map**, as text, with unexplored cells masked to `?`. That is
  Cogmind's own notion of what the player knows -- no shadowcasting, no
  partial-occlusion modelling, no glyph decoding.
* The recent message log, per-map stats, and discovered exits.
* **A turn counter.** `stats.exploration.turnsPassed`, plus per-action-type
  counts in `stats.actions.total`, globally and per map. No monotonic turn
  counter was ever found in memory -- a candidate at 0x00D3C258 went 7->8 then
  8->7 -- so this is the only reliable source.

`map.lines` geometry (verified, not assumed)
--------------------------------------------
50 rows of 50 characters, a window **centred on the player**, so

    game_x = col + player_x - 25
    game_y = row + player_y - 25

Confirmed against the cell table: a dump with the player at (58,51) put `<` at
(row 26, col 26), and probing (59,52) returned `STAIRS_YRD`, glyph `<`. The
2:1-downsample reading of the same grid is ruled out -- it would have placed the
player at col 29, not col 25.

Two consequences worth keeping in mind: the window is 50x50 on maps that are
100x100, so it is a *local* view of the known map rather than all of it; and
because it is centred on the player, the origin moves every time the player
does. Behaviour at a map edge is untested.

Keys are camelCase
------------------
`jsonScoresheet` / `jsonStatDump` emit camelCase (`totalScore`, `runResult`),
*not* the snake_case of the embedded schema. Looking up proto field names
returns null silently. `harness/extract_proto.py` recovers the schema itself
from COGMIND.exe.
"""

import argparse
import glob
import json
import os
import sys

PROFILE = os.path.expanduser("~/Documents/Cogmind-bench")
# Never touch the player's own profile; the benchmark runs against an isolated
# one. Same guard as episode.py's clear_saves, for the same reason.
PLAYER_PROFILE = os.path.expanduser("~/Documents/Cogmind")

# map.lines is a square window centred on the player.
MAP_WINDOW = 50
MAP_CENTRE = MAP_WINDOW // 2
UNKNOWN = "?"


def wine_path_to_host(p):
    """Translate the path the writer returns into a host path.

    It comes back mixed, e.g.
    `Z:\\Users\\heni\\Documents\\Cogmind-bench/dumps/cogbench-...txt`: the
    profile part carries the separators of the `-customFilePath` we passed in,
    the rest are the game's own. Both are normalised here.
    """
    p = p.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = p[2:]  # drop the Wine drive letter; Z: is the host root
    return p


def latest(profile=PROFILE, ext="json"):
    """Newest dump under the profile, or None."""
    files = glob.glob(os.path.join(profile, "dumps", "*." + ext))
    return max(files, key=os.path.getmtime) if files else None


def load(path):
    with open(path) as f:
        return json.load(f)


def prune(profile=PROFILE, keep=20):
    """Delete all but the newest `keep` dumps.

    Each dump is ~30 KB of text plus ~11 KB of JSON and nothing rotates them, so
    an agent that dumps once a turn leaves thousands of files over a run. Call
    this between episodes.
    """
    if os.path.realpath(profile) == os.path.realpath(PLAYER_PROFILE):
        raise SystemExit("refusing to prune the player's own profile: %s" % profile)
    files = sorted(
        glob.glob(os.path.join(profile, "dumps", "*")),
        key=os.path.getmtime,
        reverse=True,
    )
    # Two files per dump, so keep twice as many paths as dumps requested.
    doomed = files[keep * 2 :]
    for f in doomed:
        os.unlink(f)
    return len(doomed)


# ------------------------------------------------------------------------ map


def map_grid(dump, player_x, player_y):
    """`map.lines` as (origin_x, origin_y, rows).

    Needs the player's position because the window is centred on it and the
    dump does not record where the window starts. Take the position from the
    same instant as the dump -- the player record at 0x00D2D338, or `<`/`@`
    within the grid itself.
    """
    lines = dump.get("map", {}).get("lines", [])
    return (player_x - MAP_CENTRE, player_y - MAP_CENTRE, lines)


def find_glyph(dump, glyph):
    """Every game coordinate holding `glyph`, resolved through the player's own
    position in the grid rather than an assumed origin -- `@` is in the window,
    so the grid can locate itself."""
    lines = dump.get("map", {}).get("lines", [])
    at = None
    for r, line in enumerate(lines):
        c = line.find("@")
        if c >= 0:
            at = (c, r)
            break
    if at is None:
        return []
    out = []
    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            if ch == glyph:
                out.append((c - at[0], r - at[1]))  # offsets from the player
    return out


def known_cells(dump):
    """How much of the window is explored: (known, total)."""
    lines = dump.get("map", {}).get("lines", [])
    total = sum(len(line) for line in lines)
    unknown = sum(line.count(UNKNOWN) for line in lines)
    return total - unknown, total


# ----------------------------------------------------------------- observation


def observation(dump):
    """The dump reduced to what an agent needs, with the noise dropped.

    Empty sub-objects are the norm rather than an error: proto3 omits default
    values, so `temperature: {}` means "heat 0, no thermoelectric network", not
    "missing".
    """
    cog = dump.get("cogmind", {})
    loc = cog.get("location", {})
    parts = dump.get("parts", {})
    game = dump.get("game", {})
    known, total = known_cells(dump)

    def var(name, default_max=0):
        v = cog.get(name, {})
        return {
            "current": v.get("current", 0),
            "maximum": v.get("maximum", default_max),
        }

    return {
        "run": {
            "seed": game.get("worldSeed"),
            "seed_manual": game.get("worldSeedIsManual", False),
            "run_time": game.get("runTime"),
            "result_so_far": dump.get("header", {}).get("runResult"),
            "build": dump.get("header", {}).get("build"),
        },
        "location": {"depth": loc.get("depth", 0), "map": loc.get("map", "MAP_NONE")},
        "resources": {
            "core_integrity": var("coreIntegrity"),
            "matter": var("matter"),
            "energy": var("energy"),
            # proto3 omits zeros, so an absent corruption/heat block is a zero.
            "corruption": cog.get("corruption", {}).get("value", 0),
            "heat": cog.get("temperature", {}).get("value", 0),
        },
        "movement": {
            "mode": cog.get("movement", {}).get("mode"),
            "speed": cog.get("movement", {}).get("speed", 0),
            "overweight_factor": cog.get("movement", {}).get("overweightFactor", 0),
        },
        "parts": {
            sect: {
                "slots": parts.get(sect, {}).get("slots", 0),
                "attached": parts.get(sect, {}).get("parts", []),
            }
            for sect in ("power", "propulsion", "utility", "weapon", "inventory")
        },
        "turns": {
            "passed": dump.get("stats", {})
            .get("exploration", {})
            .get("turnsPassed", 0),
            "actions": dump.get("stats", {}).get("actions", {}).get("total", {}),
            "spaces_moved": dump.get("stats", {})
            .get("exploration", {})
            .get("spacesMoved", {})
            .get("overall", 0),
        },
        "messages": dump.get("lastMessages", {}).get("messages", []),
        "map": {
            "window": MAP_WINDOW,
            "known_cells": known,
            "window_cells": total,
            "exits_relative_to_player": find_glyph(dump, "<"),
        },
        "route": [
            {
                "depth": e.get("location", {}).get("depth"),
                "map": e.get("location", {}).get("map"),
            }
            for e in dump.get("route", {}).get("entries", [])
        ],
    }


# ------------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["obs", "map", "raw", "path", "prune"])
    ap.add_argument("file", nargs="?", help="dump .json (default: newest)")
    ap.add_argument("--profile", default=PROFILE)
    ap.add_argument(
        "--player", help="PLAYER_X,PLAYER_Y, to label map rows with " "game coordinates"
    )
    ap.add_argument(
        "--keep", type=int, default=20, help="dumps to keep when pruning (default 20)"
    )
    a = ap.parse_args()

    if a.cmd == "prune":
        print("removed %d file(s)" % prune(a.profile, a.keep))
        return

    path = a.file or latest(a.profile)
    if not path:
        raise SystemExit("no dumps under %s/dumps -- call stat_dump first" % a.profile)

    if a.cmd == "path":
        print(path)
        return

    dump = load(path)
    if a.cmd == "raw":
        json.dump(dump, sys.stdout, indent=1)
        print()
    elif a.cmd == "obs":
        json.dump(observation(dump), sys.stdout, indent=1)
        print()
    else:
        lines = dump.get("map", {}).get("lines", [])
        if a.player:
            px, py = (int(v) for v in a.player.split(","))
            ox, oy = px - MAP_CENTRE, py - MAP_CENTRE
            print(
                "origin (%d,%d); game_x = col + %d, game_y = row + %d"
                % (ox, oy, ox, oy)
            )
            for r, line in enumerate(lines):
                print("%3d |%s|" % (oy + r, line))
        else:
            print("no --player given, so rows are window-relative")
            for r, line in enumerate(lines):
                print("%3d |%s|" % (r, line))


if __name__ == "__main__":
    main()
