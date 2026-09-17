#!/usr/bin/env python3
"""
Join Cogmind's part names to Cog-Minder's item table, and encode build rules.

The stat dump names every attached part and every inventory item exactly --
"Lgt. Treads", "Sml. Storage Unit", "Assault Rifle" -- and those strings are
keys in `cog-minder/src/json/items.json` (1343 entries). So the agent can know
what it is wearing, not merely that a slot is occupied.

That turned out to matter more than combat. Both baseline runs died having lost
five parts and attached nothing, with working weapons in inventory; the problem
was never that they fought badly, it was that they arrived at every fight naked.

The rules below are a player's, not derived -- see notes/b17.1-luigiai.md
round 17. They are deliberately narrow and cheap to check, because the point is
to find out whether a small table of expert rules moves the score at all before
building a retrieval system to hold a large one.
"""

import json
import os

COG_MINDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cog-minder", "src", "json"
)

# Cogmind's combat classes -- the damage types that stack usefully. Kinetic is
# the recommended default: ballistic guns are the most common early drop, so
# a kinetic pair is the easiest to actually assemble.
PREFERRED_DAMAGE = "Kinetic"


class Itemdex:
    def __init__(self, path=None):
        path = path or os.path.join(COG_MINDER, "items.json")
        with open(path) as f:
            raw = json.load(f)
        # Index on BOTH names. The UI and items.json use the abbreviated form
        # ("Lgt. Treads"); the stat dump reports the expanded one
        # ("Light Treads"), and items.json carries that as `Full Name`. Keying
        # on only one silently matches nothing -- the first version of these
        # rules never fired a single time for exactly that reason, with a full
        # inventory sitting right there.
        self.items = {}
        for i in raw:
            for key in ("Name", "Full Name"):
                if i.get(key):
                    self.items.setdefault(i[key], i)

    def __len__(self):
        return len(self.items)

    def get(self, name):
        return self.items.get(name)

    def _num(self, name, field, default=0):
        it = self.items.get(name) or {}
        try:
            return int(str(it.get(field, default)).split("~")[0])
        except (TypeError, ValueError):
            return default

    def slot(self, name):
        return (self.items.get(name) or {}).get("Slot")

    def type(self, name):
        return (self.items.get(name) or {}).get("Type")

    def damage_type(self, name):
        return (self.items.get(name) or {}).get("Damage Type")

    def rating(self, name):
        return self._num(name, "Rating")

    def support(self, name):
        """Treads carry the most mass per slot, which is what makes them the
        Materials answer: you can be slow there, but you cannot be over
        capacity and still carry salvage out."""
        return self._num(name, "Support")

    def mass(self, name):
        return self._num(name, "Mass")

    def is_launcher(self, name):
        return (self.type(name) or "").endswith("Launcher")

    def is_storage(self, name):
        return self.type(name) == "Storage"

    def is_treads(self, name):
        return self.type(name) == "Treads"


# ------------------------------------------------------------------ the rules


def best_in_inventory(dex, inventory, predicate, key):
    """Highest-`key` inventory entry matching `predicate`, as (index, name)."""
    best = None
    for i, n in enumerate(inventory):
        if predicate(dex, n) and (best is None or key(dex, n) > key(dex, best[1])):
            best = (i, n)
    return best


def treads_in_materials(dex, obs, sit):
    """Wear treads in Materials.

    MAT is where salvage is dense and the walk is short, so carrying capacity
    beats speed. Only fires on that map, and only if treads are actually in the
    inventory."""
    if obs["location"]["map"] != "MAP_MAT":
        return None
    worn = obs["parts"]["propulsion"]["attached"]
    if worn and all(dex.is_treads(n) for n in worn):
        return None
    pick = best_in_inventory(
        dex,
        obs["parts"]["inventory"]["attached"],
        lambda d, n: d.is_treads(n),
        lambda d, n: d.support(n),
    )
    return ("equip", pick[0], pick[1], "treads for Materials") if pick else None


def biggest_storage(dex, obs, sit):
    """Take the largest storage unit available, accepting overweight in MAT.

    Inventory capacity is what lets a run leave Materials stronger than it
    arrived. Being overweight costs speed, which is the cheapest thing to spend
    on that floor."""
    utility = obs["parts"]["utility"]
    worn = [n for n in utility["attached"] if dex.is_storage(n)]
    pick = best_in_inventory(
        dex,
        obs["parts"]["inventory"]["attached"],
        lambda d, n: d.is_storage(n),
        lambda d, n: d.rating(n),
    )
    if not pick:
        return None
    if worn and max(dex.rating(n) for n in worn) >= dex.rating(pick[1]):
        return None
    if len(utility["attached"]) >= utility["slots"] and not worn:
        return None  # no room and nothing to displace
    return ("equip", pick[0], pick[1], "larger storage unit")


def stack_damage_type(dex, obs, sit):
    """Run one damage type across both weapon slots.

    Mixed damage splits every resistance calculation and wastes the parts that
    would have stacked. Kinetic is the default because ballistic guns are the
    commonest early drop."""
    weapon = obs["parts"]["weapon"]
    worn = weapon["attached"]
    if len(worn) >= weapon["slots"]:
        types = {dex.damage_type(n) for n in worn if dex.damage_type(n)}
        if len(types) <= 1:
            return None  # already stacked
    pick = best_in_inventory(
        dex,
        obs["parts"]["inventory"]["attached"],
        lambda d, n: d.slot(n) == "Weapon" and d.damage_type(n) == PREFERRED_DAMAGE,
        lambda d, n: d.rating(n),
    )
    return ("equip", pick[0], pick[1], "stack %s" % PREFERRED_DAMAGE) if pick else None


def pocket_launcher(dex, obs, sit):
    """Keep launchers in the inventory, not on the frame.

    A launcher is a moment, not a loadout: you equip it to break up a swarm and
    stow it again. Worn full-time it is dead weight and a friendly-fire hazard,
    so this rule only equips one when genuinely swarmed."""
    inv = obs["parts"]["inventory"]["attached"]
    worn = obs["parts"]["weapon"]["attached"]
    swarmed = (sit.get("hostiles") or 0) >= 3 and sit.get("under_attack")
    if swarmed:
        pick = best_in_inventory(
            dex, inv, lambda d, n: d.is_launcher(n), lambda d, n: d.rating(n)
        )
        if pick and not any(dex.is_launcher(n) for n in worn):
            return ("equip", pick[0], pick[1], "launcher for the swarm")
    return None


BUILD_RULES = [
    ("pocket_launcher", pocket_launcher),
    ("stack_damage_type", stack_damage_type),
    ("treads_in_materials", treads_in_materials),
    ("biggest_storage", biggest_storage),
]


def next_build_action(dex, obs, sit):
    """First applicable build rule, as (rule_name, action_tuple)."""
    for name, fn in BUILD_RULES:
        try:
            got = fn(dex, obs, sit)
        except Exception:
            continue
        if got:
            return name, got
    return None, None


if __name__ == "__main__":
    dex = Itemdex()
    print("loaded %d items" % len(dex))
    for n in (
        "Lgt. Treads",
        "Sml. Storage Unit",
        "Assault Rifle",
        "EM Pulse Gun",
        "Sml. Laser",
    ):
        print(
            "  %-20s slot=%-11s type=%-15s dmg=%-12s support=%-4s rating=%s"
            % (
                n,
                dex.slot(n),
                dex.type(n),
                dex.damage_type(n),
                dex.support(n),
                dex.rating(n),
            )
        )
