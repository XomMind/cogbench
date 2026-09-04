#!/usr/bin/env python3
"""
What to hack at a terminal, in what order, and why.

Hacking is a scoring dimension no policy here has touched, and it is the one
where a table of expert priorities should pay best: the hacks are a fixed menu
with fixed base odds, so "which of these is worth a try right now" is exactly
the kind of question a lookup answers better than a 4B model reasoning from an
ASCII screen.

Odds come from `cog-minder/src/json/machine_hacks.json` (7 machine types, 87
Terminal hacks). Ordering is a player's -- see notes round 17 -- not derived
from the odds, and in several places it deliberately contradicts them: the
highest-probability hack is usually not the most valuable one.

The clearest case is traps. `Traps(Locate)` is the likeliest to land at 60% and
is worth nothing, because knowing where a trap is does not stop it hurting you.
`Traps(Disarm)` at 45% removes the array; `Traps(Reprogram)` at 30% turns it on
the things chasing you. Sorting this menu by success rate picks the useless one.

## Status

The *choices* below are encoded and testable. **Driving the terminal UI is
not** -- `CMD_DOMAIN_HACK` exposes only navigation (scroll, page, home/end,
close, F2 cursor toggle), so hacks are selected by typing their name, and this
harness has never driven a terminal. `LuigiAi.machineHacking` going non-NULL is
the one UI domain we can positively detect, which makes this the right place to
start on domain tracking.
"""

import json
import os
import re

COG_MINDER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cog-minder", "src", "json")


class Hackdex:
    def __init__(self, path=None):
        path = path or os.path.join(COG_MINDER, "machine_hacks.json")
        with open(path) as f:
            raw = json.load(f)
        self.machines = {g["Name"]: g for g in raw}

    def hacks(self, machine="Terminal"):
        return (self.machines.get(machine) or {}).get("Hacks", [])

    def chance(self, name, machine="Terminal"):
        for h in self.hacks(machine):
            if h["Name"] == name:
                return h.get("BaseChance")
        return None

    def analysis_chance(self, tier):
        """`Analysis` odds fall 6 points per bot tier: 54% at tier 1 down to 0%
        at tier 10. Tier comes from botdex, so the two tables join -- knowing
        what you are fighting tells you whether analysing it is even possible."""
        return self.chance("Analysis([Bot Name]) - tier %d" % max(1, min(10, tier)))

    def find(self, pattern, machine="Terminal"):
        rx = re.compile(pattern, re.I)
        return [h for h in self.hacks(machine) if rx.search(h["Name"])]


# --------------------------------------------------------------- the priority

# Each entry: (hack, guard(state) -> bool, why).
# Tried in order; the first whose guard passes and which the terminal actually
# offers is the one to attempt.
#
# `state` carries: depth (negative, ascending toward the surface), map, the bot
# names seen on this floor, and whether traps or a garrison are known.

HACK_PRIORITY = [
    # Direct effects first. These change the floor; the information hacks only
    # describe it.
    ("Traps(Reprogram)", lambda s: s.get("traps_known"),
     "turns a trap array on the things chasing you"),
    ("Traps(Disarm)", lambda s: s.get("traps_known"),
     "removes the array outright -- Locate only tells you where it is"),
    ("Layout(Zone)", lambda s: True,
     "reveals this terminal's zone, which is the exploration the agent is "
     "otherwise paying for one step at a time"),

    # Then the analyses, cheapest tiers first. Swarmers before Grunts: swarms
    # are what actually kill an under-built Cogmind, and both are tier 1 so the
    # odds are identical -- the ordering is about which knowledge matters.
    ("Analysis(Swarmer)", lambda s: True, "swarms are the early-run killer"),
    ("Analysis(Grunt)", lambda s: True, "the commonest armed class"),

    # Hauler locations, so they can be camped rather than stumbled into.
    ("Enumerate(Transport)", lambda s: (s.get("depth") or -11) >= -9,
     "haulers carry parts; camping them from -9 up is free salvage"),

    # Branch access becomes the run-defining hack in the upper Complex.
    ("Access(Branch)", lambda s: (s.get("depth") or -11) >= -7,
     "branches make a run stronger, and some lock out later if missed"),
]

# Explicitly not worth a hack attempt on its own.
DECLINED = {
    "Traps(Locate)": "knowing where a trap is does not stop it hurting you",
    "Index(Machines)": "0% base chance",
    "Analysis([Bot Name]) - tier 10": "0% base chance",
}


def hack_plan(dex, state, offered=None, machine="Terminal"):
    """The ordered list of hacks worth attempting here, as (name, chance, why).

    A plan rather than a single choice, because a terminal is not one-shot: you
    keep hacking until you are traced, so what matters is the *order* you spend
    those attempts in. Returning one hack made the highest-priority entry shadow
    everything below it forever -- `Layout(Zone)` always won and `Access(Branch)`
    never fired, which is precisely backwards in the upper Complex.

    `Access(Branch)` is promoted to the front from -7 up. It is the hack that
    changes what a run can become, and unlike the others a missed branch can be
    locked out permanently -- so it is worth spending the *first* attempt on
    even at 30%, when a later attempt might never come.
    """
    plan = []
    for name, guard, why in HACK_PRIORITY:
        if name in DECLINED:
            continue
        try:
            if not guard(state):
                continue
        except Exception:
            continue
        if offered is not None and name not in offered:
            continue
        chance = dex.chance(name, machine)
        if chance is None and name.startswith("Analysis"):
            chance = dex.analysis_chance(state.get("bot_tier", 1))
        plan.append((name, chance, why))

    depth = state.get("depth")
    if depth is not None and depth >= -7:
        plan.sort(key=lambda e: e[0] != "Access(Branch)")
    return plan


def next_hack(dex, state, offered=None, machine="Terminal"):
    """First entry of the plan, or None."""
    plan = hack_plan(dex, state, offered, machine)
    return plan[0] if plan else None


if __name__ == "__main__":
    dex = Hackdex()
    print("machines: %s" % ", ".join(dex.machines))
    print("Terminal hacks: %d\n" % len(dex.hacks()))
    for depth, traps in ((-11, False), (-9, True), (-7, False)):
        st = {"depth": depth, "traps_known": traps, "bot_tier": 1}
        print("depth %-4d traps=%s" % (depth, traps))
        for i, (n, c, why) in enumerate(hack_plan(dex, st), 1):
            print("   %d. %-22s %3s%%  %s" % (i, n, c, why[:56]))
        print()
    print()
    for n in ("Enumerate(Transport)", "Access(Branch)", "Traps(Disarm)",
              "Traps(Locate)", "Layout(Zone)"):
        print("  %-24s %s%%" % (n, dex.chance(n)))
