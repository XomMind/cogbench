#!/usr/bin/env python3
"""
Generate Cogbench's action space from Cogmind's own binding tables.

Cogmind writes two files into its user directory when `exposeKeybinds=1` is set
in advanced.cfg:

  keyboard.cfg   one line per SDL keycode: "<keycode> <default_name> <current_name>"
                 The line index IS the SDLK_* value, so this is an authoritative
                 name -> keysym table for the exact build in use.

  commands.cfg   every command in the game, grouped into [CMD_DOMAIN_*] blocks,
                 each binding carrying its Ctrl / Shift / Alt / Key.

Together they are the complete input grammar: the set of legal actions, which UI
mode each is legal in, and the exact keystroke that triggers it. Generating the
action space from them means a version bump produces a diff instead of a
debugging session.

Both files are CRLF.

Usage:
  gen_actions.py --user-dir ~/Documents/Cogmind/user -o actions.json
  gen_actions.py --user-dir ... --report
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

# SDL 1.2 modifier bits (SDL_keysym.mod). We emit left-hand variants; Cogmind
# checks for either side, and SDL_GetModState() is set to match on the shim side.
KMOD_NONE = 0x0000
KMOD_LSHIFT = 0x0001
KMOD_LCTRL = 0x0040
KMOD_LALT = 0x0100

DOMAIN_RE = re.compile(r"^\[(CMD_DOMAIN_[A-Z_0-9]+)\]")
BIND_RE = re.compile(
    r'^(?P<command>CMD_[A-Z_0-9]+)\s+'
    r'"(?P<label>[^"]*)"\s+'
    r'(?P<ctrl>\S+)\s+'
    r'(?P<shift>\S+)\s+'
    r'(?P<alt>\S+)\s+'
    r'(?P<key>\S+)\s*$'
)

# Single-character key names whose unicode differs under shift. Cogmind reads
# text through SDL_keysym.unicode, which the old shim always left at 0.
SHIFT_MAP = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&",
    "8": "*", "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}",
    "\\": "|", ";": ":", "'": '"', ",": "<", ".": ">", "/": "?", "`": "~",
}

# keyboard.cfg spells punctuation out; map the names back to characters so we can
# compute a unicode codepoint for text entry.
NAME_TO_CHAR = {
    "SPACE": " ", "EXCLAMATION": "!", "DOUBLEQUOTE": '"', "HASH": "#",
    "DOLLAR": "$", "PERCENT": "%", "AMPERSAND": "&", "QUOTE": "'",
    "LEFTPARENTHESIS": "(", "RIGHTPARENTHESIS": ")", "ASTERISK": "*",
    "PLUS": "+", "COMMA": ",", "MINUS": "-", "PERIOD": ".", "SLASH": "/",
    "COLON": ":", "SEMICOLON": ";", "LESS": "<", "EQUALS": "=", "GREATER": ">",
    "QUESTION": "?", "AT": "@", "LEFTBRACKET": "[", "BACKSLASH": "\\",
    "RIGHTBRACKET": "]", "CARET": "^", "UNDERSCORE": "_", "BACKQUOTE": "`",
    "RETURN": "\r", "TAB": "\t", "BACKSPACE": "\b",
}


def read_crlf(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return [ln.rstrip("\r\n") for ln in f]


def parse_keyboard(path):
    """line index -> SDLK value; returns {name: keysym} and {keysym: name}."""
    by_name, by_sym = {}, {}
    for ln in read_crlf(path):
        if ln.startswith("//") or not ln.strip():
            continue
        parts = ln.split()
        if len(parts) < 2:
            continue  # keycode with no binding
        try:
            sym = int(parts[0])
        except ValueError:
            continue
        default = parts[1]
        current = parts[2] if len(parts) > 2 else default
        by_sym[sym] = current
        # register both spellings; commands.cfg refers to them by name
        by_name.setdefault(default, sym)
        by_name.setdefault(current, sym)
    return by_name, by_sym


def unicode_for(key_name, shift):
    ch = NAME_TO_CHAR.get(key_name)
    if ch is None and len(key_name) == 1:
        ch = key_name
    if ch is None:
        return 0  # named key with no text meaning (F1, UP, KP8, ...)
    if shift:
        if ch.isalpha():
            ch = ch.upper()
        else:
            ch = SHIFT_MAP.get(ch, ch)
    return ord(ch)


def binding_cost(b):
    """Lower is a better canonical choice: fewest modifiers, then a plain letter
    key over an arrow or keypad key, then original file order."""
    mods = b["ctrl"] + b["shift"] + b["alt"]
    exotic = 0 if len(b["key"]) == 1 else 1
    return (mods, exotic, b["order"])


def parse_commands(path, keysyms):
    domains = defaultdict(list)
    commands = {}
    unresolved = []
    domain = None
    order = 0

    for ln in read_crlf(path):
        m = DOMAIN_RE.match(ln)
        if m:
            domain = m.group(1)
            continue
        m = BIND_RE.match(ln)
        if not m or domain is None:
            continue
        g = m.groupdict()
        key = g["key"]
        ctrl = g["ctrl"] != "-"
        shift = g["shift"] != "-"
        alt = g["alt"] != "-"

        sym = keysyms.get(key)
        if sym is None:
            unresolved.append((domain, g["command"], key))

        mods = KMOD_NONE
        if ctrl:
            mods |= KMOD_LCTRL
        if shift:
            mods |= KMOD_LSHIFT
        if alt:
            mods |= KMOD_LALT

        b = {
            "command": g["command"],
            "label": g["label"],
            "domain": domain,
            "ctrl": ctrl, "shift": shift, "alt": alt,
            "key": key,
            "keysym": sym,
            "mods": mods,
            "unicode": unicode_for(key, shift),
            "order": order,
        }
        order += 1
        domains[domain].append(b)
        commands.setdefault(g["command"], {"domain": domain, "bindings": []})
        commands[g["command"]]["bindings"].append(b)

    # canonical binding per command: one unambiguous keystroke to emit
    for name, c in commands.items():
        usable = [b for b in c["bindings"] if b["keysym"] is not None]
        c["canonical"] = min(usable, key=binding_cost) if usable else None

    return domains, commands, unresolved


def report(domains, commands, unresolved, keysyms):
    total_binds = sum(len(v) for v in domains.values())
    print(f"domains          {len(domains)}")
    print(f"commands         {len(commands)}")
    print(f"bindings         {total_binds}")
    print(f"keysym names     {len(keysyms)}")
    nocanon = [n for n, c in commands.items() if c["canonical"] is None]
    print(f"no canonical     {len(nocanon)}")
    if unresolved:
        print(f"\nunresolved key names ({len(unresolved)}):")
        for d, c, k in unresolved[:20]:
            print(f"   {d:30} {c:40} {k}")
        if len(unresolved) > 20:
            print(f"   ... +{len(unresolved)-20} more")
    if nocanon:
        print("\ncommands with no usable binding:")
        for n in nocanon[:20]:
            print(f"   {n}")

    # keystroke collisions inside one domain: the same chord bound to two commands
    print("\nintra-domain keystroke collisions:")
    found = 0
    for d, binds in sorted(domains.items()):
        seen = {}
        for b in binds:
            if b["keysym"] is None:
                continue
            k = (b["keysym"], b["mods"])
            if k in seen and seen[k] != b["command"]:
                print(f"   {d}: {b['key']} mods=0x{b['mods']:04x} -> "
                      f"{seen[k]} AND {b['command']}")
                found += 1
            seen.setdefault(k, b["command"])
    if not found:
        print("   (none)")

    print("\nper-domain command counts:")
    for d, binds in sorted(domains.items(), key=lambda kv: -len(kv[1])):
        cmds = len({b["command"] for b in binds})
        print(f"   {d:34} {cmds:4} commands  {len(binds):4} bindings")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-dir", default=os.path.expanduser("~/Documents/Cogmind/user"))
    ap.add_argument("-o", "--out")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    kb = os.path.join(a.user_dir, "keyboard.cfg")
    cm = os.path.join(a.user_dir, "commands.cfg")
    for p in (kb, cm):
        if not os.path.exists(p):
            print(f"missing {p}\nSet exposeKeybinds=1 in advanced.cfg and run the game once.",
                  file=sys.stderr)
            return 2

    keysyms, by_sym = parse_keyboard(kb)
    domains, commands, unresolved = parse_commands(cm, keysyms)

    if a.report or not a.out:
        report(domains, commands, unresolved, keysyms)

    if a.out:
        for c in commands.values():
            for b in c["bindings"]:
                b.pop("order", None)
            if c["canonical"]:
                c["canonical"].pop("order", None)
        doc = {
            "source": {"keyboard_cfg": kb, "commands_cfg": cm},
            "keysyms": keysyms,
            "keysym_names": {str(k): v for k, v in by_sym.items()},
            "domains": {d: sorted({b["command"] for b in v}) for d, v in domains.items()},
            "commands": commands,
        }
        with open(a.out, "w") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
        print(f"\nwrote {a.out}: {len(commands)} commands, {len(domains)} domains")
    return 0


if __name__ == "__main__":
    sys.exit(main())
