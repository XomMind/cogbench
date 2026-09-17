#!/usr/bin/env python3
"""
Join Cogmind's combat log to Cog-Minder's bot stat blocks.

Why the combat log
------------------
The fair view names nothing: `map.lines` draws robots as bare letters, and the
cell table stores an entity *handle* (`(generation << 16) | slot`) with no
pointer and no array we have located yet. So an agent can see that something is
there and not what it is -- which is how a baseline run opened fire on an R-06
Scavenger, a salvage derelict that posed no threat.

The combat log does name things, in every stat dump, for free:

    Sml. Laser (50%) Hit
      R-06 Sml. Storage Unit damaged: 12

Those names join exactly to `cog-minder/src/json/bots.json` -- `G-34 Mercenary`,
the bot that killed both baseline runs, is a literal key there with
`Core Integrity 15`. So the log tells us what we are fighting and Cog-Minder
tells us how much it can take.

The limitation is honest and worth stating: the log only names what you trade
fire with, so this gives the *cast* of a floor, not a positional map. Names tied
to coordinates need the entity struct -- which, per Plexion, is also where the
per-entity FOV maps live, so it stays worth finding.

Matching is by longest exact name against the 324-entry table rather than by
regex. A pattern loose enough to catch `K-01 Serf` also catches `Systems
online...` and `Loading variables...`, which is exactly what the first version
did.
"""

import json
import os
import re

COG_MINDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cog-minder", "src", "json"
)


class Botdex:
    def __init__(self, path=None):
        path = path or os.path.join(COG_MINDER, "bots.json")
        with open(path) as f:
            raw = json.load(f)
        self.bots = {b["Name"]: b for b in raw}
        # Longest first, so "K-01 Serf" wins over a hypothetical "K-01".
        self._ordered = sorted(self.bots, key=len, reverse=True)
        self._re = re.compile("|".join(re.escape(n) for n in self._ordered))

    def __len__(self):
        return len(self.bots)

    def find(self, text):
        """Every bot name occurring in a block of log text, in order."""
        seen, out = set(), []
        for m in self._re.finditer(text or ""):
            n = m.group(0)
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def stats(self, name):
        """The fields a tactical script actually wants. `Core Integrity` is the
        one that matters most: it is how many points of damage the thing takes
        to kill, and it is often far lower than its behaviour suggests."""
        b = self.bots.get(name)
        if not b:
            return None

        def num(k, default=0):
            v = b.get(k, default)
            try:
                return int(str(v).split("~")[0])
            except (TypeError, ValueError):
                return default

        return {
            "name": name,
            "class": b.get("Class"),
            "tier": num("Tier"),
            "threat": num("Threat"),
            "rating": num("Rating"),
            "core_integrity": num("Core Integrity"),
            "core_exposure_pct": num("Core Exposure %"),
            "speed": num("Speed"),
            "sight": num("Sight Range"),
            "armament": b.get("Armament String") or b.get("Armament"),
            "resistances": b.get("Resistances"),
            "immunities": b.get("Immunities"),
            # A bot with no armament cannot shoot back. That single fact would
            # have kept the first smoke test from emptying its guns into a
            # scavenger.
            "unarmed": not (b.get("Armament String") or b.get("Armament")),
        }


if __name__ == "__main__":
    import sys

    dex = Botdex()
    print("loaded %d bots" % len(dex))
    for n in sys.argv[1:] or ["G-34 Mercenary", "R-06 Scavenger", "K-01 Serf"]:
        s = dex.stats(n)
        print("\n%s" % n)
        if not s:
            print("  not found")
            continue
        for k, v in s.items():
            print("  %-18s %s" % (k, str(v)[:70]))
