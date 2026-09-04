#!/usr/bin/env python3
"""
Local-model Cogmind agent -- the first non-scripted baseline.

The question this answers is not "can a 4B play Cogmind". It is **"does a 4B
beat bot.py"**, which scored 2470 by walking to stairs, killing nothing and
dying with almost nothing attached. That is the floor, and it is a low one:
picking parts up would clear it.

## Three design choices, and why

**Macro-actions, not keystrokes.** `descend 8` runs BFS and walks up to eight
steps, stopping early if a hostile appears or the way is blocked. One inference
per *decision*, not per tile -- a run is thousands of turns and a 4B doing one
forward pass each would take days and learn nothing. The model spends its
attention on fight/flee/loot/descend, which is where the headroom is; the shell
does the pathing, which is where small models fall apart.

**A fair view by default.** Terrain comes from the stat dump's `map.lines` --
Cogmind's own known map, unexplored cells masked to `?`. bot.py reads the cell
table instead, which is ground truth and shows robots through walls, so its
2470 is an upper bound rather than a baseline. `--cheat` switches to the cell
table when you want the two numbers side by side.

**A heuristic control.** `--policy heuristic` runs the same macro-actions with
no model at all: pick up what you are standing on, otherwise descend. Without
it, a good score cannot be attributed -- the macro-actions alone might be doing
the work, and that is the more likely explanation for any modest gain over
bot.py.

## Grammar-constrained output

Generation is constrained by a GBNF grammar to exactly the action set, so
"model emitted prose" is not a failure mode that can occur. This is deliberately
the same mechanism the project wants for its Lua shell, at a smaller vocabulary:
whatever is learned here about grammar shape transfers.

## The context argument

An observation is roughly a thousand tokens and a run is thousands of turns, so
history cannot be kept. Every decision is made from the current observation
alone. That is not a limitation being worked around -- the stat dump is close to
a sufficient statistic (resources with maxima, full loadout, known map, recent
log, turn count), which makes the environment near-Markovian and a fixed-state
recurrent policy the natural fit for it.
"""

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import Bot, STEP          # noqa: E402  BFS, movement, UI clearing
from cogbench import Statmind      # noqa: E402
import episode                     # noqa: E402
import statdump                    # noqa: E402
from botdex import Botdex          # noqa: E402

DIRS = {
    "n": (0, -1), "ne": (1, -1), "e": (1, 0), "se": (1, 1),
    "s": (0, 1), "sw": (-1, 1), "w": (-1, 0), "nw": (-1, -1),
}
STEP_CHOICES = ("1", "2", "4", "8", "12")

# Real keysyms, from actions.json rather than guessed.
K_FIRE = 102        # CMD_BS_DEFAULT_FIRE and CMD_BS_TARGETING_FIRE are both 'f'
K_GET_ATTACH = 97   # 'a' -- get *and* attach, repeating if slots are full
K_WAIT = 261        # KP5
K_ASCEND = 60       # '<'
K_TAB = 9           # CMD_BS_TARGETING_NEXT_TARGET
K_TARGET_CANCEL = 120  # 'x' -- CMD_BS_TARGETING_CANCEL


# ------------------------------------------------------------------ fair view

# Glyph classes in `map.lines`. Cogmind draws floor as a space and walls as `#`;
# robots are letters. Everything else -- machines, items, debris -- is drawn with
# assorted punctuation and is *assumed walkable*, which is wrong for machines.
# That is deliberate: a move into a machine simply fails, and the blocked-cell
# blacklist inherited from bot.py routes around it on the next plan. Erring
# toward "try it" costs one wasted turn; erring toward "wall" can make a floor
# look unreachable and strand the agent.
GLYPH_UNKNOWN = "?"
GLYPH_WALL = "#"
GLYPH_PLAYER = "@"
GLYPH_EXITS = "<>"
GLYPH_DOORS = "+/'"


class FairView:
    """What the player knows, accumulated across stat dumps.

    `map.lines` is a 50x50 window centred on the player (verified against the
    cell table three times, most recently at 472/472 cells), so it is a *local*
    view: a map is 75x75 or 100x100, and anything outside the window is absent
    from the dump whether or not the player has been there.

    That makes a per-dump view memoryless in a way the player is not. An
    exit walked past ten steps ago simply vanishes, so a policy built on single
    dumps explores forever and never descends -- which is exactly what the
    heuristic control did: 399 explores, one descend, 609 steps, no second exit
    ever found.

    So glyphs accumulate into `world`, keyed by absolute game coordinate, and
    terrain persists until the floor changes. Entities are deliberately *not*
    accumulated: robots move, and a remembered robot is worse than no robot.

    This is the one place the stat dump is not a sufficient statistic. Resources,
    parts and status are complete in every dump; the map is not, and either the
    harness accumulates it or a policy has to carry it in state.
    """

    def __init__(self, dump, player_x, player_y, world=None):
        self.lines = dump.get("map", {}).get("lines", [])
        self.ox = player_x - statdump.MAP_CENTRE
        self.oy = player_y - statdump.MAP_CENTRE
        self.player = (player_x, player_y)
        self.world = world if world is not None else {}

        self.entities = {}
        self.objects = {}

        # Fold this window into the accumulated map. A robot standing on a cell
        # hides the terrain under it, so letters are recorded as plain floor --
        # otherwise the map fills with phantom obstacles as robots wander.
        for r, line in enumerate(self.lines):
            for c, ch in enumerate(line):
                p = (self.ox + c, self.oy + r)
                if ch == GLYPH_UNKNOWN:
                    continue
                if ch.isalpha() and ch != GLYPH_PLAYER:
                    self.entities[p] = ch
                    self.world[p] = " "
                    continue
                if ch not in " #" + GLYPH_PLAYER + GLYPH_DOORS + GLYPH_EXITS:
                    self.objects[p] = ch
                # `@` covers whatever the player is standing on, so letting it
                # overwrite the accumulated glyph would erase the one fact
                # "should I pick this up?" depends on. Remember the terrain.
                if ch == GLYPH_PLAYER and self.world.get(p, " ") not in (" ", GLYPH_PLAYER):
                    continue
                self.world[p] = ch

        self.passable = {p for p, ch in self.world.items() if ch != GLYPH_WALL}
        self.exits = [p for p, ch in self.world.items() if ch in GLYPH_EXITS]
        # "Unknown" is now everything adjacent to known ground that has never
        # been seen, rather than everything outside the current window.
        self.unknown = set()
        for (x, y) in self.passable:
            for dx, dy in DIRS.values():
                q = (x + dx, y + dy)
                if q not in self.world:
                    self.unknown.add(q)

    def frontier(self, min_dist=5):
        """Known-walkable cells that touch an unexplored one -- where exploring
        actually reveals something. Pathing straight at `?` cells would route
        into the unknown; pathing to their walkable neighbours does not.

        `min_dist` exists because the cell you just stepped onto is usually
        itself adjacent to unknown space, so a nearest-frontier goal is
        satisfied after a single step and the agent dithers, burning one
        decision per tile. Preferring distant frontier pulls it across the map
        instead; the filter is dropped when it would leave nothing to aim at.
        """
        px, py = self.player
        out = []
        for (x, y) in self.passable:
            for dx, dy in DIRS.values():
                if (x + dx, y + dy) in self.unknown:
                    out.append((x, y))
                    break
        far = [p for p in out
               if max(abs(p[0] - px), abs(p[1] - py)) >= min_dist]
        return far or out

    def crop(self, half=6):
        """A small square around the player, for the prompt. The full 50x50 is
        ~2500 characters and would dominate the token budget; the model is not
        meant to path from it anyway."""
        px, py = self.player
        rows = []
        for y in range(py - half, py + half + 1):
            row = []
            for x in range(px - half, px + half + 1):
                if (x, y) == (px, py):
                    row.append("@")
                    continue
                c, r = x - self.ox, y - self.oy
                if 0 <= r < len(self.lines) and 0 <= c < len(self.lines[r]):
                    row.append(self.lines[r][c])
                else:
                    row.append(GLYPH_UNKNOWN)
            rows.append("".join(row))
        return rows


def bearing(frm, to):
    """Compass direction and Chebyshev distance -- the metric that matches
    8-way movement, so `distance` is literally the number of steps."""
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    ns = "n" if dy < 0 else ("s" if dy > 0 else "")
    ew = "w" if dx < 0 else ("e" if dx > 0 else "")
    return (ns + ew) or "here", max(abs(dx), abs(dy))


# ------------------------------------------------------------------- the model

GRAMMAR = r'''
root    ::= action
action  ::= descend | explore | pickup | attach | fire | flee | move | wait
descend ::= "descend " steps
explore ::= "explore " steps
flee    ::= "flee " steps
move    ::= "move " dir " " steps
fire    ::= "fire " dir
attach  ::= "attach " [0-7]
pickup  ::= "pickup"
wait    ::= "wait " steps
dir     ::= "nw" | "ne" | "sw" | "se" | "n" | "e" | "s" | "w"
steps   ::= "1" | "2" | "4" | "8" | "12"
'''

SYSTEM = """You are playing Cogmind, a roguelike. You are a robot that rebuilds \
itself from the parts of robots it destroys.

Goal: reach lower depth numbers. Depth -11 is deep, -1 is the surface. Taking an \
exit (<) moves you one floor up. You win by escaping.

What kills runs: having no weapons, having no propulsion, fighting things that \
outgun you, and standing still while damaged.

Reply with exactly one action and nothing else."""

ACTIONS_HELP = """actions:
  descend N   path toward the nearest known exit, up to N steps
  explore N   path toward unexplored space, up to N steps
  flee N      move away from the nearest hostile, up to N steps
  move D N    move D up to N steps (D = n ne e se s sw w nw)
  fire D      fire your weapons toward D
  pickup      pick up and attach whatever you are standing on
  attach K    attach inventory item K
  wait N      pass N turns
N must be one of 1 2 4 8 12."""


class Llama:
    """llama.cpp server client.

    Uses `/completion` with the chat template applied by hand rather than
    `/v1/chat/completions`: the native endpoint's `grammar` support is the
    documented one, and the OpenAI-compatible path has historically differed
    between builds on whether it forwards the field.
    """

    def __init__(self, url, temperature=0.7, timeout=180):
        self.url = url.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.calls = 0
        self.total_ms = 0
        self.prompt_tokens = 0

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def health(self):
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=10) as r:
                return json.load(r)
        except Exception as e:
            raise SystemExit(
                "no llama-server at %s (%s)\n"
                "start one with:\n"
                "  llama-server -m models/gemma-4-E4B_q4_0-it.gguf "
                "-c 8192 -ngl 999 --port 8080" % (self.url, e))

    def act(self, prompt):
        t0 = time.time()
        out = self._post("/completion", {
            "prompt": prompt,
            "grammar": GRAMMAR,
            "n_predict": 12,
            "temperature": self.temperature,
            "cache_prompt": True,
        })
        self.calls += 1
        self.total_ms += int((time.time() - t0) * 1000)
        self.prompt_tokens += out.get("tokens_evaluated", 0)
        return out.get("content", "").strip()


def gemma_prompt(system, user):
    """Gemma's chat template. It has no system role, so the system text is
    folded into the first user turn -- which is what the official template
    does too."""
    return ("<start_of_turn>user\n%s\n\n%s<end_of_turn>\n<start_of_turn>model\n"
            % (system, user))



# ------------------------------------------------------------------- scripts

# Ten hand-written tactical scripts, tried in priority order, first guard wins.
#
# This is the cheap test of the retrieval thesis. If a *hash table* of ten rules
# beats the 200 that both the model and the trivial heuristic scored, then a
# library of situational scripts is worth building and an embedding index is an
# optimisation of something already known to work. If it does not beat 200,
# retrieval would not have saved it and the problem is elsewhere.
#
# They target the measured failure rather than clever play: across 90 model
# decisions and 400 heuristic ones, neither policy fired a shot, fled, or picked
# anything up -- both died holding working weapons, having dealt 7 damage, all
# of it accidental ramming. None of these rules need a bot name, which is why
# they can land before the entity struct is found.

def _pct(v):
    return v["current"] / v["maximum"] if v.get("maximum") else 1.0


SCRIPTS = [
    ("flee_critical",
     lambda s: s["integrity"] < 0.35 and s["under_attack"],
     "flee 8"),

    # The dossier says nothing named on this floor is armed, so combat is pure
    # cost: heat, energy, and turns not spent descending. This is the rule that
    # would have saved twenty decisions from an R-06 Scavenger.
    ("ignore_harmless",
     lambda s: s["all_harmless"] and not s["under_attack"],
     "explore 12"),

    ("fire_adjacent",
     lambda s: s["armed"] and s["can_fire"] and s["under_attack"]
               and s["near"] is not None and s["near"] <= 1,
     "fire n"),

    ("flee_outnumbered",
     lambda s: s["under_attack"] and s["hostiles"] >= 3
               and s["near"] is not None and s["near"] <= 5,
     "flee 8"),

    ("fire_in_range",
     lambda s: s["armed"] and s["can_fire"] and s["under_attack"]
               and s["near"] is not None and s["near"] <= 6,
     "fire n"),

    ("pickup_underfoot",
     lambda s: s["on_item"],
     "pickup"),

    ("flee_unarmed",
     lambda s: not s["armed"] and s["under_attack"],
     "flee 8"),

    ("descend_standing",
     lambda s: s["on_exit"],
     "descend 1"),

    ("descend_known",
     lambda s: s["exit_route"],
     "descend 12"),

    ("wait_regen",
     lambda s: s["energy"] < 0.25 and s["near"] is None,
     "wait 4"),

    ("explore",
     lambda s: True,
     "explore 12"),
]


# -------------------------------------------------------------------- theagent

class Agent(Bot):
    def __init__(self, sm, llama=None, policy="model", cheat=False, verbose=True):
        super().__init__(sm, verbose=verbose)
        self.llama = llama
        self.policy = policy
        self.cheat = cheat
        self.view = None
        self.world = {}
        self.world_depth = None
        # How many times a move into each cell has failed, this floor.
        self.fails = collections.Counter()
        self.decisions = 0
        self.action_counts = collections.Counter()
        self.script_counts = collections.Counter()
        self.last_script = None
        self.dex = Botdex()
        self.prev_damage_taken = 0
        self.prev_volleys = 0
        self.fire_misfires = 0
        # Generic stall detection. Three separate livelocks have now come from
        # the same root cause -- a script whose guard stays true while its
        # action changes nothing: firing into an open targeting panel (19 of 20
        # decisions), re-attempting an impassable cell (378 of 400), and
        # picking up an item already taken (269 of 400, 363 in a row). Each was
        # patched individually and the next one appeared somewhere else.
        #
        # So instead: watch whether the *world* moved. If a script runs three
        # times without any observable consequence, suppress it for a while and
        # let a lower-priority one act. This catches the instances not yet
        # found, and it is the rule the Lua API should enforce on every
        # primitive -- an action that claims to have acted can prove it, because
        # the stat dump already counts turns, volleys, damage, parts and spaces
        # moved.
        self.prev_sig = None
        self.stall = collections.Counter()
        self.suppressed = {}
        self.threat_ttl = 0
        self.seen_names = []
        self.invalid = 0

    # ------------------------------------------------------------ observation

    def refresh(self):
        """One dump per decision. That is the natural rhythm: the dump *is* the
        observation, and at 17-109ms it is cheaper than the inference that
        follows it by two orders of magnitude."""
        pl = self.settled_player()
        if not pl.get("plausible"):
            return None, None
        raw = self.sm.tool("stat_dump")
        info = json.loads(raw) if isinstance(raw, str) else raw
        path = statdump.wine_path_to_host(info.get("returned") or "")
        js = path[:-4] + ".json" if path.endswith(".txt") else path
        if not os.path.exists(js):
            return None, None
        dump = statdump.load(js)
        # A cell is blacklisted when a move into it fails, which usually means
        # a hostile was standing there rather than a wall -- walking into one
        # spends the turn attacking and leaves the position unchanged, which is
        # indistinguishable from terrain out here. That fact expires: the robot
        # moves. So the blacklist lives for one decision, long enough for
        # walk_toward to route around what it just bumped, and no longer.
        # Keeping it for the whole run slowly makes the floor unroutable.
        # Blacklist lifetime, which took two wrong answers to get right.
        #
        # A failed move means one of two things needing opposite handling: a
        # hostile stood there (transient -- it walks away) or the cell is
        # genuinely impassable (a machine, a sealed door). Blacklisting forever
        # degrades pathing as stale entity-blocks pile up; clearing every
        # decision livelocks on real obstacles -- a control run spent 378 of 400
        # decisions re-attempting the same cell, (21,95), because each refresh
        # forgave it.
        #
        # So: strikes. One failure is forgiven at the next decision, two make it
        # permanent for the floor. Transient blocks expire, real ones stick.
        self.blocked = {c for c, n in self.fails.items() if n >= 2}
        # A new floor is a new map: coordinates are reused, so carrying terrain
        # across a descent would path the agent through the previous level.
        depth = dump.get("cogmind", {}).get("location", {}).get("depth")
        loc = (depth, dump.get("cogmind", {}).get("location", {}).get("map"))
        if loc != self.world_depth:
            self.world = {}
            self.fails.clear()
            self.blocked = set()
            self.world_depth = loc
        self.view = FairView(dump, pl["x"], pl["y"], world=self.world)
        if self.cheat:
            # Ground truth for terrain, the dump still for everything else.
            self.load_map()
            self.view.passable = self.passable
            self.view.exits = self.exits
        else:
            self.passable = self.view.passable
            self.exits = self.view.exits
            self.entities = dict.fromkeys(self.view.entities, 1)
        return dump, pl

    def render(self, dump, pl):
        o = statdump.observation(dump)
        me = (pl["x"], pl["y"])
        L = []
        L.append("depth %s %s   turn %s"
                 % (o["location"]["depth"], o["location"]["map"],
                    o["turns"]["passed"]))
        r = o["resources"]
        L.append("core %d/%d  matter %d/%d  energy %d/%d  corruption %d  heat %d"
                 % (r["core_integrity"]["current"], r["core_integrity"]["maximum"],
                    r["matter"]["current"], r["matter"]["maximum"],
                    r["energy"]["current"], r["energy"]["maximum"],
                    r["corruption"], r["heat"]))

        for sect in ("power", "propulsion", "utility", "weapon"):
            p = o["parts"][sect]
            got = p["attached"]
            L.append("%-11s %d/%d  %s" % (sect, len(got), p["slots"],
                                          ", ".join(got) or "EMPTY"))
        inv = o["parts"]["inventory"]
        L.append("inventory   %s" % (", ".join(
            "%d:%s" % (i, n) for i, n in enumerate(inv["attached"][:8])) or "empty"))

        hostiles = sorted(((bearing(me, p)[1], bearing(me, p)[0], g)
                           for p, g in self.view.entities.items()))[:4]
        L.append("hostiles    " + (", ".join("%s %s %d" % (g, d, n)
                                             for n, d, g in hostiles) or "none"))
        objs = sorted(((bearing(me, p)[1], bearing(me, p)[0], g)
                       for p, g in self.view.objects.items()))[:4]
        L.append("objects     " + (", ".join("%s %s %d" % (g, d, n)
                                             for n, d, g in objs) or "none"))

        path = self.route(me, self.view.exits) if self.view.exits else None
        if path is not None:
            d, n = bearing(me, self.view.exits[0])
            L.append("exit        %d steps away (%s); route known" % (len(path), d))
        elif self.view.exits:
            L.append("exit        seen but no route through known ground")
        else:
            L.append("exit        not found yet -- explore")

        L.append("mapped      %d cells known, %d unexplored edges"
                 % (len(self.view.world), len(self.view.unknown)))
        L.append("log         " + " | ".join(o["messages"][-3:]))
        L.append("")
        L.append("map (@ = you, ? = unexplored, # = wall):")
        L.extend(self.view.crop(6))
        return "\n".join(L)

    # -------------------------------------------------------------- decisions

    def choose(self, obs_text, dump=None):
        if self.policy == "scripts":
            return self.scripted(dump)
        if self.policy == "heuristic":
            return self.heuristic()
        # Stable text first, observation last: llama.cpp caches the longest
        # common prefix between calls, and prefill is the whole cost here (~700
        # observation tokens at ~120 tok/s). Putting the action list before the
        # observation makes it cached instead of re-evaluated every decision.
        prompt = gemma_prompt(SYSTEM + "\n\n" + ACTIONS_HELP,
                              obs_text + "\n\nYour action:")
        for _ in range(3):
            a = self.llama.act(prompt)
            if parse_action(a):
                return a
            self.invalid += 1
        return "explore 8"

    def situation(self, dump):
        """The handful of facts the guards need. Deliberately small: this is the
        retrieval key, and if it needs more than this to be useful the scripts
        are the wrong abstraction."""
        o = statdump.observation(dump)
        me = self.view.player
        dists = [max(abs(p[0] - me[0]), abs(p[1] - me[1]))
                 for p in self.view.entities]
        r = o["resources"]

        # A letter glyph is not a hostile. Most robots in the Scrapyard are
        # derelicts -- the first smoke test opened fire on an R-06 Scavenger, a
        # salvage bot that was no threat, and burned twenty decisions on it.
        # Nothing in the fair view distinguishes them, so hostility is inferred
        # from the only unambiguous evidence: taking damage. That also encodes
        # the right policy -- do not start fights, but fight back -- and it
        # decays, so we stop shooting once whatever hit us is gone.
        combat = dump.get("stats", {}).get("combat", {})

        # Did the last `fire` actually fire? 27 fire decisions in a scripted run
        # produced 2 volleys: the guard uses Chebyshev distance with no line of
        # sight, and `map.lines` shows *explored* terrain, so it was shooting at
        # robots behind walls and at ones remembered from twenty turns ago.
        # Rather than model LOS, trust the game's own counter -- if pressing
        # fire did not raise volleysFired, stop choosing it and let a lower
        # script run. Self-correcting, and it needs no visibility model.
        volleys = combat.get("volleysFired", {}).get("overall", 0)
        if self.last_script and self.last_script.startswith("fire"):
            if volleys > self.prev_volleys:
                self.fire_misfires = 0
            else:
                self.fire_misfires += 1
        self.prev_volleys = volleys

        taken = combat.get("damageTaken", {}).get("overall", 0)
        if taken > self.prev_damage_taken:
            self.threat_ttl = 6
        elif self.threat_ttl:
            self.threat_ttl -= 1
        self.prev_damage_taken = taken

        # The combat log names whatever we trade fire with, so it is a free
        # entity-name channel while the entity struct is unfound. Matching is
        # exact against Cog-Minder's 324 bots: a regex loose enough to catch
        # "K-01 Serf" also catches "Systems online..." and "Loading
        # variables...", which is what the first version did.
        for n in self.dex.find(" | ".join(o.get("messages", []))):
            if n not in self.seen_names:
                self.seen_names.append(n)
        dossier = [self.dex.stats(n) for n in self.seen_names[-4:]]
        dossier = [d for d in dossier if d]
        # Everything named so far is unarmed -> nothing here can shoot back, so
        # there is nothing to fight or flee.
        all_harmless = bool(dossier) and all(d["unarmed"] for d in dossier)
        # Negative resistance means that damage type does *extra*. Every early
        # Cogmind bot carries Electromagnetic -25, so an EM weapon is strictly
        # the better opener when we are holding one.
        em_edge = any(str((d.get("resistances") or {})
                          .get("Electromagnetic", "0")).startswith("-")
                      for d in dossier)
        return {
            "integrity": _pct(r["core_integrity"]),
            "energy": _pct(r["energy"]),
            "armed": bool(o["parts"]["weapon"]["attached"]),
            "hostiles": len(dists),
            "near": min(dists) if dists else None,
            # `world` keeps the glyph under the player, so an item we walked
            # onto is still visible as one.
            "on_item": self.view.world.get(me, " ") in '"!=[]/*%',
            "on_exit": me in self.view.exits,
            "under_attack": self.threat_ttl > 0,
            # Two consecutive presses that produced no volley means there is
            # nothing actually shootable, whatever the distances say.
            "can_fire": self.fire_misfires < 2,
            "all_harmless": all_harmless,
            "em_edge": em_edge,
            "dossier": [d["name"] for d in dossier],
            "toughest": max([d["core_integrity"] for d in dossier] or [0]),
            "exit_route": bool(self.view.exits) and
                          self.route(me, self.view.exits) is not None,
        }

    def progress_sig(self, dump):
        """A cheap fingerprint of everything an action could plausibly change."""
        st = dump.get("stats", {})
        c = st.get("combat", {})
        e = st.get("exploration", {})
        return (
            e.get("turnsPassed", 0),
            e.get("spacesMoved", {}).get("overall", 0),
            c.get("volleysFired", {}).get("overall", 0),
            c.get("damageInflicted", {}).get("overall", 0),
            c.get("damageTaken", {}).get("overall", 0),
            st.get("build", {}).get("partsAttached", {}).get("overall", 0),
            self.view.player,
        )

    def scripted(self, dump):
        sig = self.progress_sig(dump)
        if self.last_script:
            if sig == self.prev_sig:
                self.stall[self.last_script] += 1
                if self.stall[self.last_script] >= 3:
                    # Suppress for a while rather than forever: the guard may be
                    # right again later, when the world has moved.
                    self.suppressed[self.last_script] = self.decisions + 25
                    self.stall[self.last_script] = 0
            else:
                self.stall[self.last_script] = 0
        self.prev_sig = sig

        sit = self.situation(dump)
        for name, guard, action in SCRIPTS:
            if self.suppressed.get(name, -1) > self.decisions:
                continue
            try:
                if guard(sit):
                    self.script_counts[name] += 1
                    self.last_script = name
                    return action
            except Exception:
                continue
        return "explore 12"

    def heuristic(self):
        """The control: pick up what you are standing on, otherwise descend,
        otherwise explore. Deliberately stupid -- it exists to show how much of
        any score comes from the macro-actions rather than the model."""
        me = self.view.player
        if me in self.view.objects:
            return "pickup"
        if self.view.exits and self.route(me, self.view.exits) is not None:
            return "descend 12"
        return "explore 12"

    # ----------------------------------------------------------- macro-actions

    def walk_toward(self, goals, budget, label):
        """Path to the nearest goal and walk it, stopping on anything that
        invalidates the plan. Interrupts are the point: a plan made eight steps
        ago is stale the moment something walks into view."""
        moved = 0
        for _ in range(budget):
            me = self.view.player
            if me in goals:
                return moved, "%s: arrived" % label
            # Staged relaxation, which bot.py learned the hard way and this
            # dropped when it reimplemented pathing. A strict plan treats every
            # robot as a permanent wall, so four of them standing in one
            # corridor makes the entire map unroutable: measured live, strict
            # routing returned None while the same query with entities allowed
            # returned a five-step path, and 344 of 400 decisions in a control
            # run died as "no route". Walking into a hostile is a real cost --
            # it spends the turn attacking -- so prefer to avoid them, but
            # never let them veto the plan outright.
            path = None
            for avoid, blist in ((True, True), (False, True), (False, False)):
                path = self.route(me, goals, avoid_entities=avoid,
                                  use_blacklist=blist)
                if path:
                    break
            if not path:
                return moved, "%s: no route" % label
            ok, _msg = self.move_to(path[0])
            if not ok:
                self.fails[path[0]] += 1
                self.blocked.add(path[0])
                return moved, "%s: blocked at %s (strike %d)" % (
                    label, path[0], self.fails[path[0]])
            moved += 1
            pl = self.settled_player(tries=3, delay=0.15)
            self.view.player = (pl["x"], pl["y"])
            if self.view.player in goals:
                return moved, "%s: arrived" % label
        return moved, "%s: %d steps" % (label, moved)

    def do(self, action):
        verb, *rest = action.split()
        self.action_counts[verb] += 1
        me = self.view.player

        if verb == "descend":
            n = int(rest[0])
            if me in self.view.exits:
                self.key(K_ASCEND, K_ASCEND)
                return "took the exit"
            if not self.view.exits:
                return self.walk_toward(self.view.frontier(), n, "no exit, explore")[1]
            moved, msg = self.walk_toward(self.view.exits, n, "descend")
            if self.view.player in self.view.exits:
                self.key(K_ASCEND, K_ASCEND)
                return msg + "; took the exit"
            return msg

        if verb == "explore":
            f = self.view.frontier()
            if not f:
                return "explore: nothing unexplored in view"
            return self.walk_toward(f, int(rest[0]), "explore")[1]

        if verb == "flee":
            if not self.view.entities:
                return "flee: nothing to flee from"
            # Walk to the reachable known cell that maximises distance from the
            # nearest hostile -- crude, but it beats stepping at random.
            near = min(self.view.entities,
                       key=lambda p: max(abs(p[0] - me[0]), abs(p[1] - me[1])))
            far = sorted(self.view.passable,
                         key=lambda p: -max(abs(p[0] - near[0]), abs(p[1] - near[1])))
            return self.walk_toward(set(far[:20]), int(rest[0]), "flee")[1]

        if verb == "move":
            d, n = rest[0], int(rest[1])
            dx, dy = DIRS[d]
            moved = 0
            for _ in range(n):
                ok, _ = self.move_to((self.view.player[0] + dx,
                                      self.view.player[1] + dy))
                if not ok:
                    break
                moved += 1
                pl = self.settled_player(tries=3, delay=0.15)
                self.view.player = (pl["x"], pl["y"])
            return "move %s: %d steps" % (d, moved)

        if verb == "fire":
            # `f` enters CMD_DOMAIN_BS_TARGETING with the cursor already on the
            # nearest target, and `f` again is CMD_BS_TARGETING_FIRE. The old
            # version sent `f` then a *direction* then RETURN -- but in
            # targeting the letters are cursor movement and RETURN is
            # ADD_WAYPOINT, so it placed a waypoint and never fired a shot.
            # That is why 90 decisions produced 7 damage, all of it ramming.
            self.key(K_FIRE, K_FIRE, pause=0.35)
            self.key(K_FIRE, K_FIRE, pause=0.45)
            # Leave CMD_DOMAIN_BS_TARGETING explicitly. Firing does not always
            # close it, and a stuck targeting cursor swallows every later key:
            # a smoke test spent 19 of 20 decisions pressing `f` into an open
            # targeting panel, firing one volley in total.
            self.key(K_TARGET_CANCEL, K_TARGET_CANCEL, pause=0.3)
            return "fire"

        if verb == "pickup":
            # GET_ATTACH ('a'), not GET ('g'): it picks the part up *and* fits
            # it, repeating if slots are full. Plain GET leaves it in inventory,
            # which is where both baseline runs' parts stayed.
            self.key(K_GET_ATTACH, K_GET_ATTACH, pause=0.4)
            # Forget the item remembered on this cell, unconditionally.
            # `world` keeps the glyph under the player so the agent can tell it
            # is standing on something -- but nothing was clearing it after the
            # pickup, so the guard stayed true forever: one run spent 269 of its
            # decisions here, 363 of them consecutively. Clearing is right
            # either way. If the pickup worked the item is gone; if it failed
            # (slots full, or there was nothing there) we cannot take it, so it
            # should stop being a reason to act.
            self.view.world[me] = " "
            return "pickup"

        if verb == "wait":
            for _ in range(int(rest[0])):
                self.key(K_WAIT, 0, pause=0.2)   # KP5, not the '5' key
            return "wait %s" % rest[0]

        return "unhandled: %s" % action

    def screenshot(self, why=""):
        """F12. When the agent cannot tell what screen it is on, this is the
        only diagnostic that answers the question directly -- the blit capture
        gives positions but glyph decoding was ruled out, so the rendered PNG
        is the readable one."""
        self.sm.tool("key", {"keysym": 293})
        time.sleep(1.5)
        self.say("  screenshot (%s) -> <profile>/screenshots/" % why)

    # ------------------------------------------------------------------- loop

    def play(self, max_decisions=200):
        watcher = episode.RunWatcher()
        self.wake()
        start = None
        stuck = 0
        t0 = time.time()

        for i in range(max_decisions):
            dump, pl = self.refresh()
            if dump is None:
                # `plausible == False` means the base screen does not have focus
                # -- a floor transition, the evolution screen, or a panel. It
                # does NOT mean dead: the handle is zeroed by any modal.
                stuck += 1
                self.say("[%3d] no base screen (attempt %d) -- clearing" % (i, stuck))
                self.clear_blocking_ui()
                end = watcher.poll()
                if end:
                    return self.finish(end, start, t0, "run ended")
                if stuck >= 12:
                    self.screenshot("stuck")
                    return self.finish(end, start, t0,
                                       "could not reach the base screen; "
                                       "screenshot written to the profile")
                continue
            stuck = 0

            depth = dump["cogmind"]["location"].get("depth", 0)
            if start is None:
                start = depth
            obs = self.render(dump, pl)
            action = self.choose(obs, dump)
            self.decisions += 1
            result = self.do(action)
            self.say("[%3d] d%-4s %-16s %-12s -> %s"
                     % (i, depth, self.last_script or "-", action, result))

            end = watcher.poll()
            if end:
                return self.finish(end, start, t0, "run ended")

        return self.finish(watcher.poll(), start, t0, "decision budget spent")

    def finish(self, end, start, t0, why):
        out = {
            "why": why,
            "decisions": self.decisions,
            "steps": self.steps,
            "start_depth": start,
            "actions": dict(self.action_counts),
            "scripts": dict(self.script_counts),
            "invalid_generations": self.invalid,
            "fire_misfires": self.fire_misfires,
            "suppressions": {k: v for k, v in self.suppressed.items()},
            "names_seen": self.seen_names[:20],
            "wall_seconds": round(time.time() - t0, 1),
            "result": end,
        }
        if self.llama:
            out["model"] = {
                "calls": self.llama.calls,
                "mean_ms": self.llama.total_ms // max(1, self.llama.calls),
                "prompt_tokens": self.llama.prompt_tokens,
            }
        return out


def parse_action(s):
    return bool(re.fullmatch(
        r"(descend|explore|flee|wait) (1|2|4|8|12)"
        r"|move (n|ne|e|se|s|sw|w|nw) (1|2|4|8|12)"
        r"|fire (n|ne|e|se|s|sw|w|nw)|pickup|attach [0-7]", s.strip()))


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--statmind", default=os.environ.get(
        "STATMIND", "/Users/heni/genAI/cogbench/StatMind/target/release/statmind"))
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--policy", choices=["model", "heuristic", "scripts"],
                    default="model")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--decisions", type=int, default=200)
    ap.add_argument("--cheat", action="store_true",
                    help="terrain from the cell table (ground truth) instead of "
                         "the known map -- comparable to bot.py, not a fair run")
    ap.add_argument("--out", help="write the result JSON here")
    a = ap.parse_args()

    llama = None
    if a.policy == "model":
        llama = Llama(a.url, temperature=a.temperature)
        llama.health()

    sm = Statmind(a.statmind, quiet=True)
    agent = Agent(sm, llama=llama, policy=a.policy, cheat=a.cheat)
    res = agent.play(max_decisions=a.decisions)
    print(json.dumps(res, indent=1))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
