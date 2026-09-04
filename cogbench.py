#!/usr/bin/env python3
"""
cogbench -- a shell over the StatMind/LuigiAI Cogmind harness.

Two halves:

  cogbench.py daemon     holds the statmind --mcp subprocess open and serves a
                         unix socket. Must keep running: statmind caches the
                         mach task port and the mailbox address, and reacquiring
                         them is slow (and on macOS re-prompts for auth).

  cogbench.py <command>  one-shot client. Connects, sends a command, prints the
                         reply, exits. This is what an agent shells out to.

  cogbench.py repl       interactive client, for driving it by hand.

The command surface is deliberately a *shell*, not a tool protocol: it is the
placeholder for the Lua grammar, so keep the verbs stable as that lands.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading

import statdump

SOCK = os.environ.get("COGBENCH_SOCK", "/tmp/cogbench.sock")

DIRS = {
    "n": "move_north",  "ne": "move_northeast", "e": "move_east", "se": "move_southeast",
    "s": "move_south",  "sw": "move_southwest", "w": "move_west", "nw": "move_northwest",
}

ACTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actions.json")

# Friendly names for the gameplay commands worth having a short verb for.
# Everything else is reachable as `cmd <CMD_NAME>`; run `actions` to list them.
ALIASES = {
    "get":       "CMD_BS_DEFAULT_GET",
    "getattach": "CMD_BS_DEFAULT_GET_ATTACH",
    "wait":      "CMD_BS_DEFAULT_WAIT",
    "autopath":  "CMD_BS_DEFAULT_KEYBOARD_AUTOPATH",
    "exits":     "CMD_BS_DEFAULT_LABEL_EXITS",
    "enemies":   "CMD_BS_DEFAULT_LABEL_ENEMIES",
    "partslabel":"CMD_BS_DEFAULT_LABEL_PARTS",
    "status":    "CMD_BS_DEFAULT_STATUS",
    "intel":     "CMD_BS_DEFAULT_INTEL",
    "dumpmap":   "CMD_BS_DEFAULT_OUTPUT_MAP",
    "detachall": "CMD_BS_DEFAULT_DETACH_ALL",
    "worldmap":  "CMD_BS_DEFAULT_WORLD_MAP",
    "up":        "CMD_BS_DEFAULT_MOVE_UP",
}

RUN_DIRS = {d: f"CMD_BS_DEFAULT_RUN_{d.upper()}" for d in DIRS}


def load_actions():
    if not os.path.exists(ACTIONS_PATH):
        return None
    with open(ACTIONS_PATH) as f:
        return json.load(f)


HELP = """commands:
  look [w] [h]     render the map around Cogmind (default 80x30)
  state            raw LuigiAI state as JSON (tile list is empty on b17.1)
  probe <x> <y>    dump one cell's raw bytes + decoded fields (calibration)
  parts            attached parts and cargo, from LuigiEntity.inventory
  who              entities currently in FOV, with coordinates
  move <dir>       n ne e se s sw w nw
  fire | attach    the two bound action keys
  cmd <CMD_NAME>   send any command from commands.cfg by name (329 of them)
  actions [filter] list commands, optionally filtered by name or domain
  run <dir>        run in a direction until something happens
  key <sym> [c][s][a]   raw keysym with optional ctrl/shift/alt
  text <string>    type text (hacking codes, seeds) with correct unicode
  mouse <x> <y>    warp the cursor
  click <x> <y> [b]     warp and click
  dump [obs|path]  make Cogmind serialise the run in progress and read it back
                   (parts, resource maxima, known map, messages) -- see statdump.py
  raw <tool> [json]     call any statmind MCP tool by name
  help | quit

aliases: """ + " ".join(sorted(ALIASES)) + """
"""

# ---------------------------------------------------------------- rendering

def _glyph(cell_name):
    """Cell names are composed at runtime from a prefix plus a map tag
    (FLOOR_SAN = 'FLOOR_' + 'SAN'), so match on the prefix."""
    if cell_name is None:
        return " "
    n = cell_name
    if n.startswith(("WALL_", "BARRIER_", "SHORTCUT_", "TEMP_WALL")):
        return "#"
    if n.startswith("PHASEWALL_"):
        return "%"
    if n.startswith("DOOR_") or n.startswith("SEALED_DOOR"):
        return "+"
    if n.startswith("STAIRS_"):
        return ">"
    if n.startswith("FLOOR_") or n in ("GROUND",):
        return "."
    if n.startswith("EARTH"):
        return " "
    return "?"


def render(pl, dmap, vw=80, vh=30):
    """Everything here comes from the game's own structures, not from LuigiAI.

    On Beta 17.1 the LuigiAI tile mirror is a stub and `LuigiAi.player` stays
    NULL, so terrain comes from the cell table and the player from the
    fixed-address player record."""
    w = dmap["width"]
    h = dmap["height"]
    px, py = pl["x"], pl["y"]
    phandle = pl["handle"]

    grid = [[" "] * w for _ in range(h)]
    marks = []

    for c in dmap["cells"]:
        x, y = c["x"], c["y"]
        if not (0 <= x < w and 0 <= y < h):
            continue
        # Cell+0x04 is the game's own render character, so no glyph table.
        g = c["glyph"]
        if c["prop"]:
            g = "&"
        if c["entity"]:
            if c["entity"] == phandle:
                g = "@"
            else:
                g = "r"
                marks.append((x, y, c["entity"]))
        grid[y][x] = g

    if px >= 0 and py >= 0:
        grid[py][px] = "@"

    cx = px if px >= 0 else w // 2
    cy = py if py >= 0 else h // 2
    x0 = max(0, min(cx - vw // 2, max(0, w - vw)))
    y0 = max(0, min(cy - vh // 2, max(0, h - vh)))

    out = ["".join(grid[y][x0:x0 + vw]) for y in range(y0, min(h, y0 + vh))]

    header = (
        f"pos=({px},{py})   map={w}x{h}   view=({x0},{y0})   "
        f"cells={dmap['cell_count']} types={dmap['type_count']}   "
        f"player handle=0x{phandle:08X} ({pl['entity_name']})"
    )
    status = (
        "stats unavailable: LuigiAi.player is NULL on b17.1 and the player's "
        "integrity/matter/energy globals are not located yet"
    )
    legend = ("legend: @ you  r robot  & prop  (space) no cell; "
              "all other glyphs are the game's own")
    body = "\n".join(out)
    seen = ""
    if marks:
        rows = [f"  ({x},{y}) entity handle 0x{e:08x}" for x, y, e in marks[:40]]
        seen = ("\nrobots on the map (" + str(len(marks)) + "):\n" + "\n".join(rows))
        if len(marks) > 40:
            seen += f"\n  ... +{len(marks) - 40} more"
        seen += ("\n  note: these come from Cell+0x48 and are NOT FOV-filtered -- "
                 "this is ground truth, not what a player would see.")
    return f"{header}\n{status}\n\n{body}\n\n{legend}{seen}"


def render_parts(state):
    inv = state.get("inventory") or []
    if not inv:
        return (f"inventory empty or unreadable "
                f"(inventory_size={state['player']['inventory_size']}, "
                f"item_stride={state['item_stride']}). "
                f"If this looks wrong, try STATMIND_ITEM_STRIDE=8.")
    eq = [i for i in inv if i["equipped"]]
    cargo = [i for i in inv if not i["equipped"]]
    def fmt(items):
        return "\n".join(f"  {i['name'] or '#' + str(i['raw_id'])}  integrity={i['integrity']}"
                         for i in items) or "  (none)"
    return (f"stride={state['item_stride']}  size={state['player']['inventory_size']}\n"
            f"attached:\n{fmt(eq)}\ncargo:\n{fmt(cargo)}")


def render_who(state):
    now = state["action_ready"]
    rows = []
    for column in state["map"]:
        for t in column:
            if t["entity"] and t["last_fov"] == now:
                e = t["entity"]
                rows.append(f"  ({t['x']},{t['y']}) {e['name'] or '#'+str(e['raw_id'])} "
                            f"rel={e['relation']} integrity={e['integrity']} "
                            f"state={e['active_state']}")
    return "\n".join(rows) if rows else "  (nothing in FOV)"


# ---------------------------------------------------------------- MCP client

class Statmind:
    """Minimal JSON-RPC-over-stdio client for `statmind --mcp`."""

    def __init__(self, binary, quiet=False):
        # statmind logs every memory read to stderr -- ~180 KB for a few hundred
        # calls, which buries anything a long-running driver prints. Interactive
        # use wants it; a bot loop does not.
        self.proc = subprocess.Popen(
            [binary, "--mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
            text=True, bufsize=1,
        )
        self._id = 0
        self._lock = threading.Lock()
        self.call("initialize", {})

    def call(self, method, params):
        with self._lock:
            self._id += 1
            req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("statmind exited -- check its stderr")
            res = json.loads(line)
            if "error" in res:
                raise RuntimeError(res["error"].get("message", "unknown error"))
            return res.get("result")

    def tool(self, name, arguments=None):
        r = self.call("tools/call", {"name": name, "arguments": arguments or {}})
        return r["content"][0]["text"]

    def state(self):
        return json.loads(self.tool("get_game_state"))

    def player(self):
        return json.loads(self.tool("player"))

    def map(self, bbox=None):
        args = {}
        if bbox:
            args = {"x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3]}
        return json.loads(self.tool("get_map", args))


# ---------------------------------------------------------------- dispatch

def dispatch(sm, line):
    parts = line.split()
    if not parts:
        return ""
    cmd, args = parts[0].lower(), parts[1:]

    if cmd in ("help", "?"):
        return HELP
    if cmd == "look":
        vw = int(args[0]) if args else 80
        vh = int(args[1]) if len(args) > 1 else 30
        pl = sm.player()
        if not pl["plausible"]:
            return (f"player record at {pl['addr']} looks wrong "
                    f"(handle=0x{pl['handle']:08X} pos=({pl['x']},{pl['y']})). "
                    f"Not in a map yet?")
        px, py = pl["x"], pl["y"]
        # Bound the read to the viewport. A full 100x100 map is ~0.4s; a viewport
        # is a fraction of that.
        bbox = (px - vw // 2 - 1, py - vh // 2 - 1, px + vw // 2 + 1, py + vh // 2 + 1)
        return render(pl, sm.map(bbox), vw, vh)

    if cmd == "probe":
        if len(args) < 2:
            return "usage: probe <x> <y>"
        return json.dumps(sm.tool("probe_cell", {"x": int(args[0]), "y": int(args[1])}), indent=1)
    if cmd == "state":
        return json.dumps(sm.state(), indent=1)
    if cmd == "parts":
        return render_parts(sm.state())
    if cmd == "who":
        pl = sm.player()
        m = sm.map()
        rows = [f"  ({c['x']},{c['y']}) handle=0x{c['entity']:08x}"
                + ("  <== you" if c["entity"] == pl["handle"] else "")
                for c in m["cells"] if c["entity"]]
        return "\n".join(rows) if rows else "  (no entities on the map)"
    if cmd == "move":
        if not args or args[0].lower() not in DIRS:
            return "usage: move <n|ne|e|se|s|sw|w|nw>"
        return sm.tool(DIRS[args[0].lower()])
    if cmd in ("fire", "attach"):
        return sm.tool(cmd)
    if cmd == "actions":
        acts = load_actions()
        if not acts:
            return f"no {ACTIONS_PATH} -- run: ./gen_actions.py -o actions.json"
        filt = args[0].upper() if args else ""
        rows = []
        for name, c in sorted(acts["commands"].items()):
            if filt and filt not in name and filt not in c["domain"]:
                continue
            b = c["canonical"]
            chord = "" if not b else "+".join(
                [m for m, on in (("Ctrl", b["ctrl"]), ("Shift", b["shift"]), ("Alt", b["alt"])) if on]
                + [b["key"]])
            rows.append(f"  {name:44} {chord:20} [{c['domain'].replace('CMD_DOMAIN_','')}]")
        if not rows:
            return f"nothing matches {filt!r}"
        head = f"{len(rows)} command(s)"
        return head + "\n" + "\n".join(rows[:200]) + (
            f"\n  ... +{len(rows)-200} more" if len(rows) > 200 else "")

    if cmd in ("cmd", "run") or cmd in ALIASES:
        acts = load_actions()
        if not acts:
            return f"no {ACTIONS_PATH} -- run: ./gen_actions.py -o actions.json"
        if cmd in ALIASES:
            target = ALIASES[cmd]
        elif cmd == "run":
            if not args or args[0].lower() not in RUN_DIRS:
                return "usage: run <n|ne|e|se|s|sw|w|nw>"
            target = RUN_DIRS[args[0].lower()]
        else:
            if not args:
                return "usage: cmd <CMD_NAME>   (see `actions`)"
            target = args[0].upper()
        c = acts["commands"].get(target)
        if not c:
            near = [n for n in acts["commands"] if target in n][:8]
            return f"unknown command {target}" + (
                "\ndid you mean:\n  " + "\n  ".join(near) if near else "")
        b = c["canonical"]
        if not b:
            return f"{target} has no usable binding"
        return sm.tool("key", {
            "keysym": b["keysym"], "ctrl": b["ctrl"], "shift": b["shift"],
            "alt": b["alt"], "unicode": b["unicode"], "repeat": 1,
        }) + f"\n  ({target} = {b['key']}, domain {c['domain']})"

    if cmd == "key":
        if not args:
            return "usage: key <keysym> [ctrl] [shift] [alt]"
        try:
            sym = int(args[0])
        except ValueError:
            acts = load_actions()
            sym = (acts or {}).get("keysyms", {}).get(args[0])
            if sym is None:
                return f"not a keysym or a key name: {args[0]}"
        flags = {f.lower() for f in args[1:]}
        shift = "shift" in flags
        # SDLK_* matches ASCII below 0x80, so a printable keysym has a unicode
        # value -- and Cogmind reads text fields through keysym.unicode.
        uni = 0
        if 0x20 <= sym < 0x7F:
            ch = chr(sym)
            uni = ord(ch.upper() if shift and ch.isalpha() else ch)
        return sm.tool("key", {"keysym": sym, "ctrl": "ctrl" in flags,
                               "shift": shift, "alt": "alt" in flags,
                               "unicode": uni})

    if cmd == "text":
        if not args:
            return "usage: text <string>"
        return sm.tool("text", {"text": line.split(None, 1)[1]})

    if cmd == "mouse":
        if len(args) < 2:
            return "usage: mouse <x> <y>"
        return sm.tool("mouse_move", {"x": int(args[0]), "y": int(args[1])})

    if cmd == "click":
        if len(args) < 2:
            return "usage: click <x> <y> [button]"
        return sm.tool("mouse_click", {"x": int(args[0]), "y": int(args[1]),
                                       "button": int(args[2]) if len(args) > 2 else 1})

    if cmd == "dump":
        # One round trip gets an observation the memory reader cannot produce:
        # resource maxima (derived from parts, so stored nowhere), the loadout
        # by name, and Cogmind's own known-map with unexplored cells masked.
        res = sm.tool("stat_dump")
        try:
            info = json.loads(res) if isinstance(res, str) else res
        except ValueError:
            return res
        txt = info.get("returned") or ""
        path = statdump.wine_path_to_host(txt)
        js = path[:-4] + ".json" if path.endswith(".txt") else path
        if not os.path.exists(js):
            # jsonStatDump=0 leaves only the text version; say so rather than
            # reporting an empty observation.
            return ("wrote %s but no JSON alongside it -- set jsonStatDump=1 in "
                    "the profile's advanced.cfg" % (path or "<no path returned>"))
        dump = statdump.load(js)
        if args and args[0] == "path":
            return js
        return json.dumps(statdump.observation(dump), indent=1)

    if cmd == "raw":
        if not args:
            return "usage: raw <tool_name> [json_args]"
        # Every tool is reachable from the shell, including ones with no verb of
        # their own: `raw read_window {"addr":"0x68397160","words":32}`.
        payload = {}
        if len(args) > 1:
            try:
                payload = json.loads(" ".join(args[1:]))
            except ValueError as e:
                return "raw: arguments must be a JSON object (%s)" % e
            if not isinstance(payload, dict):
                return "raw: arguments must be a JSON object, not %s" % type(payload).__name__
        return sm.tool(args[0], payload)
    return f"unknown command: {cmd}\n{HELP}"


# ---------------------------------------------------------------- transports

def serve(binary):
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    sm = Statmind(binary)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(8)
    print(f"cogbench daemon ready on {SOCK}", flush=True)
    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                data = conn.makefile("r").readline()
                if not data:
                    continue
                try:
                    out = dispatch(sm, json.loads(data)["cmd"])
                    reply = {"ok": True, "out": out}
                except Exception as e:  # report, never take the daemon down
                    reply = {"ok": False, "out": f"{type(e).__name__}: {e}"}
                conn.sendall((json.dumps(reply) + "\n").encode())
    finally:
        os.path.exists(SOCK) and os.unlink(SOCK)


def send(cmd):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"no daemon on {SOCK} -- start it with: cogbench.py daemon", file=sys.stderr)
        return 2
    s.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
    reply = json.loads(s.makefile("r").readline())
    print(reply["out"])
    return 0 if reply["ok"] else 1


def repl():
    print(HELP)
    while True:
        try:
            line = input("cogmind> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("quit", "exit"):
            return 0
        if line:
            send(line)


def main():
    ap = argparse.ArgumentParser(description="shell over the Cogmind LuigiAI harness")
    ap.add_argument("command", nargs="*", help="command, or 'daemon' / 'repl'")
    ap.add_argument("--statmind", default=os.environ.get("STATMIND_BIN", "statmind"),
                    help="path to the statmind binary")
    a = ap.parse_args()

    if not a.command:
        return repl()
    if a.command[0] == "daemon":
        return serve(a.statmind)
    if a.command[0] == "repl":
        return repl()
    return send(" ".join(a.command))


if __name__ == "__main__":
    sys.exit(main() or 0)
