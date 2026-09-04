#!/usr/bin/env python3
"""
Scripted descent bot -- the action surface's spec test.

The point is not to play Cogmind well. It is to answer the question that gates
everything downstream: **can a competent program get down a floor through this
action surface at all?** If it cannot, no policy trained on the same surface can
either, and the surface needs work before any model touches it.

It also produces the benchmark's floor baseline and, later, behaviour-cloning
data.

## Perfect information, deliberately

Terrain comes from the game's cell table, which is ground truth rather than field
of view. That makes this an *upper bound* on how far pathing alone gets you, not
a fair baseline -- a fair one would restrict to the blit-derived visible set.
Both numbers are worth having and they measure different things; this is the
cheaper one and the right gate test.

## One connection

Each `statmind --mcp` start re-runs the magic scans, which costs seconds. A bot
taking hundreds of steps needs a persistent connection, so this reuses
cogbench.py's stdio client rather than episode.py's one-shot helper.
"""

import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cogbench import Statmind  # noqa: E402
import episode  # noqa: E402

# (dx, dy) -> the movement tool that produces it
STEP = {
    (0, -1): "move_north", (1, -1): "move_northeast",
    (1, 0): "move_east", (1, 1): "move_southeast",
    (0, 1): "move_south", (-1, 1): "move_southwest",
    (-1, 0): "move_west", (-1, -1): "move_northwest",
}


class Bot:
    def __init__(self, sm, verbose=True):
        self.sm = sm
        self.verbose = verbose
        self.log = []
        self.steps = 0
        self.map = None
        self.passable = set()
        self.exits = []
        self.entities = {}
        # Cells a move into failed. Walking into a hostile spends the turn
        # attacking instead of moving, which is indistinguishable from a wall
        # from out here -- so remember it and route around rather than retrying.
        self.blocked = set()

    def say(self, msg):
        self.log.append(msg)
        if self.verbose:
            print(msg, flush=True)

    # ------------------------------------------------------------ observation

    def luigi(self):
        return json.loads(self.sm.tool("luigi_raw"))

    def player(self):
        return json.loads(self.sm.tool("player"))

    def settled_player(self, tries=10, delay=0.4):
        """Read the player record, tolerating a transient invalid state.

        The record goes briefly implausible **during a floor transition** -- the
        handle drops to zero while the next map loads. An earlier version treated
        that as death and reported a successful descent as a failure. Anything
        reading this record needs to wait it out rather than trust one sample.
        """
        for _ in range(tries):
            pl = self.player()
            if pl.get("plausible"):
                return pl
            time.sleep(delay)
        return pl

    def load_map(self):
        m = json.loads(self.sm.tool("get_map"))
        self.map = m
        self.passable = set()
        self.exits = []
        self.entities = {}
        for c in m["cells"]:
            p = (c["x"], c["y"])
            name = c["cell_name"] or ""
            # Doors read as impassable in the table but open when walked into,
            # so they must stay routable or most floors are unreachable.
            if c["passable"] or name.startswith("DOOR_"):
                self.passable.add(p)
            if "STAIRS" in name:
                self.exits.append(p)
            if c["entity"]:
                self.entities[p] = c["entity"]
        return m

    # ------------------------------------------------------------ pathfinding

    def route(self, start, goals, avoid_entities=True, use_blacklist=True):
        """BFS over passable cells, 8-way. Returns the cell sequence or None."""
        goals = set(goals)
        if not goals:
            return None
        blocked = set(self.blocked) if use_blacklist else set()
        blocked.discard(start)
        if avoid_entities:
            blocked |= {p for p in self.entities if p != start}
        seen = {start}
        q = collections.deque([(start, [])])
        while q:
            cur, path = q.popleft()
            if cur in goals:
                return path
            cx, cy = cur
            for dx, dy in STEP:
                nxt = (cx + dx, cy + dy)
                if nxt in seen or nxt not in self.passable or nxt in blocked:
                    continue
                seen.add(nxt)
                q.append((nxt, path + [nxt]))
        return None

    # ------------------------------------------------------------ acting

    def key(self, sym, uni=0, pause=0.3):
        self.sm.tool("key", {"keysym": sym, "unicode": uni})
        time.sleep(pause)

    def clear_blocking_ui(self, rounds=3):
        """Answer the screens that appear between floors and block everything.

        The evolution screen is the important one: it comes up on every descent,
        zeroes the player record's handle while it is open, and -- crucially --
        `CMD_EVOLVE_CONFIRM` does nothing until the evolution points have actually
        been spent. Pressing RETURN alone gets you nowhere, which is why the bot
        looked like it had died mid-run. Allocate first, then confirm.

        Slot choice here is arbitrary (whatever the cursor starts on). A bot that
        cared about playing well would choose; this one only needs to get through.
        """
        RIGHT, DOWN, RETURN, F1, ESCAPE = 275, 274, 13, 282, 27
        for _ in range(rounds):
            for _ in range(3):
                self.key(RIGHT)
            self.key(RETURN, 13)
            if self.player().get("plausible"):
                self.say("  cleared the evolution screen")
                return True
            self.key(DOWN)
            for _ in range(3):
                self.key(RIGHT)
            self.key(RETURN, 13)
            if self.player().get("plausible"):
                self.say("  cleared the evolution screen (second row)")
                return True
            # Anything else modal: transmissions, intros, popups. SPACE is
            # deliberately absent -- see wake(); it *opens* a panel on a live
            # map rather than dismissing one.
            self.key(RETURN, 13)
            self.key(F1)
            self.key(ESCAPE, 27)
            if self.player().get("plausible"):
                self.say("  cleared a modal screen")
                return True
            self.key(ESCAPE)
        return self.player().get("plausible")

    def wake(self, tries=8):
        """Advance any intro/dialogue until the player is actually placed.

        **Never send SPACE here.** On the opening screens it advances dialogue,
        but on a live map it is CMD_SPECIALCOMMANDS_START and opens the Special
        Commands panel -- after which every subsequent key is interpreted in a
        different UI domain and silently does nothing. That is exactly how an
        earlier version wedged a healthy run: it opened the panel itself, then
        read the panel's zeroed player record as "not placed yet" and kept
        pressing SPACE.

        RETURN advances dialogue without that side effect; F1 and ESCAPE close
        whatever did get opened. `player.plausible` is the signal, and note it
        means "the base screen has focus", not "alive" -- any modal panel zeroes
        the record's handle.
        """
        for i in range(tries):
            pl = self.player()
            if pl.get("plausible"):
                return pl
            self.sm.tool("key", {"keysym": 13, "unicode": 13})   # RETURN
            time.sleep(0.4)
            if self.player().get("plausible"):
                continue
            self.sm.tool("key", {"keysym": 282})                 # F1, closes Commands
            time.sleep(0.3)
            self.sm.tool("key", {"keysym": 27, "unicode": 27})   # ESCAPE, cancels panels
            time.sleep(0.3)
        return self.player()

    def move_to(self, target):
        """One step toward an adjacent cell. Returns (moved, message)."""
        pl = self.settled_player()
        d = (target[0] - pl["x"], target[1] - pl["y"])
        tool = STEP.get(d)
        if tool is None:
            return False, f"target {target} is not adjacent to ({pl['x']},{pl['y']})"
        msg = self.sm.tool(tool)
        self.steps += 1
        after = self.settled_player(tries=3, delay=0.2)
        moved = (after["x"], after["y"]) != (pl["x"], pl["y"])
        return moved, msg

    # ------------------------------------------------------------ the loop

    def descend(self, max_steps=400, replan_every=25):
        """Walk to an exit and take it. Returns a result dict."""
        watcher = episode.RunWatcher()
        lr = self.luigi()
        depth0 = lr["location_depth"]
        pl = self.settled_player()
        if not pl.get("plausible") and not self.clear_blocking_ui():
            return {"ok": False, "reason": "player never placed (stuck on a screen?)",
                    "depth": depth0, "steps": self.steps}
        pl = self.settled_player()

        self.load_map()
        self.say(f"floor depth={depth0} map={self.map['width']}x{self.map['height']} "
                 f"passable={len(self.passable)} exits={len(self.exits)} "
                 f"entities={len(self.entities)}")
        if not self.exits:
            return {"ok": False, "reason": "no stairs on this floor's cell table",
                    "depth": depth0, "steps": self.steps}

        stuck = 0
        since_replan = 0
        path = None
        self.blocked = set()
        while self.steps < max_steps:
            # Depth first: a descent is the success case and also the thing that
            # makes the player record briefly invalid.
            lr = self.luigi()
            if lr["location_depth"] != depth0:
                self.say(f"DESCENDED: depth {depth0} -> {lr['location_depth']} "
                         f"in {self.steps} steps")
                return {"ok": True, "reason": "depth changed",
                        "from_depth": depth0, "to_depth": lr["location_depth"],
                        "steps": self.steps}

            # Death is a scored run end, not an unreadable record.
            ended = watcher.poll()
            if ended is not None:
                self.say(f"RUN ENDED: {ended.get('source')} "
                         f"score={ended.get('score')} loc={ended.get('location')}")
                return {"ok": False, "reason": "run ended", "run_end": ended,
                        "depth": depth0, "steps": self.steps}

            pl = self.settled_player()
            if not pl.get("plausible"):
                # A blocking screen zeroes the handle. Try to answer it before
                # concluding anything.
                if self.clear_blocking_ui():
                    pl = self.settled_player()
                    self.load_map()
                    path = None
                else:
                    return {"ok": False,
                            "reason": "player record unreadable; no depth change, no score "
                                      "row, and the blocking-UI routine did not clear it",
                            "depth": depth0, "steps": self.steps}
            here = (pl["x"], pl["y"])

            if path is None or not path or since_replan >= replan_every:
                self.load_map()
                if not self.exits:
                    return {"ok": False, "reason": "exits vanished from the cell table",
                            "depth": depth0, "steps": self.steps}
                path = self.route(here, self.exits)
                since_replan = 0
                if path is None:
                    # Relax in stages before giving up. A robot standing in a
                    # corridor is not "no route", and the blacklist itself can
                    # sever the only corridor once it has a few cells in it.
                    for kwargs in ({"avoid_entities": False},
                                   {"avoid_entities": True, "use_blacklist": False},
                                   {"avoid_entities": False, "use_blacklist": False}):
                        path = self.route(here, self.exits, **kwargs)
                        if path:
                            self.say(f"  planned {len(path)} steps from {here} "
                                     f"with {kwargs}")
                            break
                    if path is None:
                        return {"ok": False,
                                "reason": f"no route from {here} to any of {self.exits[:4]} "
                                          f"even unrestricted",
                                "depth": depth0, "steps": self.steps}
                    self.say(f"  planned {len(path)} steps from {here} (through entities)")
                else:
                    self.say(f"  planned {len(path)} steps from {here}")

            nxt = path[0]
            moved, msg = self.move_to(nxt)
            since_replan += 1
            if moved:
                path.pop(0)
                stuck = 0
                if self.steps % 25 == 0:
                    self.say(f"  step {self.steps}: at {here}, {len(path)} to go")
            else:
                stuck += 1
                self.blocked.add(nxt)
                self.say(f"  blocked at {here} heading {nxt}; avoiding it "
                         f"({len(self.blocked)} cells blacklisted)")
                path = None
                if stuck >= 25:
                    return {"ok": False,
                            "reason": f"stuck near {here}: {stuck} failed moves, "
                                      f"{len(self.blocked)} cells blacklisted",
                            "depth": depth0, "steps": self.steps}
        return {"ok": False, "reason": f"step budget {max_steps} exhausted",
                "depth": depth0, "steps": self.steps}


def main():
    ap = argparse.ArgumentParser(description="scripted Cogmind descent bot")
    ap.add_argument("--statmind", default=os.environ.get(
        "STATMIND_BIN", "/Users/heni/genAI/cogbench/StatMind/target/release/statmind"))
    ap.add_argument("--floors", type=int, default=1, help="how many floors to descend")
    ap.add_argument("--max-steps", type=int, default=400, help="budget per floor")
    a = ap.parse_args()

    sm = Statmind(a.statmind)
    bot = Bot(sm)
    results = []
    t0 = time.time()
    for i in range(a.floors):
        print(f"\n=== floor {i + 1}/{a.floors} ===", flush=True)
        try:
            r = bot.descend(max_steps=a.max_steps)
        except Exception as e:
            r = {"ok": False, "reason": f"{type(e).__name__}: {e}", "steps": bot.steps}
        results.append(r)
        print(json.dumps(r), flush=True)
        if not r.get("ok"):
            break
        bot.steps = 0
    print(f"\n=== summary ({time.time() - t0:.0f}s, {sum(x.get('steps',0) for x in results)} steps) ===")
    for i, r in enumerate(results, 1):
        print(f"  floor {i}: {'OK  ' if r.get('ok') else 'FAIL'}  {r.get('reason')}")
    return 0 if results and results[-1].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
