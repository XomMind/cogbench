#!/usr/bin/env python3
r"""
Read the text Cogmind draws, out of the glyph cache plus the blit log.

Why there is no "cache insertion hook"
--------------------------------------
Round 12 concluded that decoding glyphs from blit source rects was impossible:
the same source rect (sx=336, sy=0) drew both `.` and `#`, and the two source
surfaces looked like caches with recycled slots. The first half of that is true
-- they *are* caches, and they do mutate -- but the conclusion was wrong, for a
reason that only shows up if you read the surfaces' pixels:

    a slot stores a FLAT RGB COLOUR with the glyph shape in the ALPHA channel.

So a slot's RGB changes constantly -- it holds whatever colour that character
was last drawn in -- while its alpha mask, the glyph itself, never changes.
Round 12 compared pixels, saw them change, and called it recycling. Measured
over a panel open and close (9.4k draws, the whole screen repainted):

    slots whose glyph changed while non-empty: 0
    slots that went empty -> filled:           0    (all of them already in use)

That makes the slot index a stable glyph id, and decoding needs no hook inside
COGMIND.exe and no change to the SDL shim. It is a pure memory read.

The layout, read off the alpha channel of the 384x240 text cache (32x10 slots
of 12x24) and confirmed by decoding a live screen into English:

    slots   0..31    ASCII 32..63          chr(32 + slot)
    slot    32       '@'
    slots  33..38    [ \ ] ^ _ `
    slots  39..47    bracket variants and the CP437 shade blocks
    slots  64..89    'A'..'Z'              chr(ord('A') + slot - 64)
    slots  96..121   'a'..'z'              (small caps in the shipped font)
    slots 128..      CP437 box drawing

Two limits worth knowing before building on this:

* **Colour does not survive.** One slot serves every colour that character is
  drawn in, repainted between blits, so by the time we read the cache it holds
  only the last one. Cogmind colour-codes hostiles, warnings and highlights, so
  if that matters the shim has to record it at blit time -- that, and not the
  glyph, is what a hook would buy.
* **Only redrawn cells appear.** Beta 17.1 dirty-rects, so the log holds what
  changed since `blit_clear`. `capture()` toggles a panel to force a full
  repaint; idle, you get one cell.

The map font (768x504, 32x21 slots of 24x24) works the same way, except each
24-wide cell is drawn as two 12-wide half blits, so its slot is sx // cell_w
with the halves folded together.
"""

import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cogbench import Statmind  # noqa: E402

BIN = os.environ.get("STATMIND_BIN", "statmind")
SHEET_COLS = 32  # every Cogmind font sheet is 32 wide
F1 = 282  # toggling a panel forces a full repaint

# SDL 1.2 SDL_Surface, 32-bit: flags, format*, w, h, pitch(u16), pixels, ...
S_W, S_H, S_PITCH, S_PIXELS = 2, 3, 4, 5


def _words(sm, addr, n):
    w = sm.tool("read_window", {"addr": "0x%08X" % addr, "words": n})
    return [x["i32"] & 0xFFFFFFFF for x in json.loads(w)["words"]]


def _bytes(sm, addr, n):
    out = bytearray()
    while len(out) < n:
        chunk = min(256, (n - len(out) + 3) // 4)
        for v in _words(sm, addr + len(out), chunk):
            out += bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, v >> 24))
    return bytes(out[:n])


def surface(sm, ptr):
    v = _words(sm, ptr, 8)
    return {
        "ptr": ptr,
        "w": v[S_W],
        "h": v[S_H],
        "pitch": v[S_PITCH] & 0xFFFF,
        "pixels": v[S_PIXELS],
    }


def find_atlases(sm, settle=1.0, nudge=True):
    """Locate the glyph caches by watching what the game blits from.

    The pointers are stable within a build (0x03D87CF0 and 0x03D87D90 on this
    one, the same values round 12 saw in a different session) but they are heap
    addresses, so they are discovered rather than baked in.

    Text and map fonts are told apart by cell shape: Cogmind's text fonts are
    twice as tall as they are wide (6x12 at 1x), its map fonts are square.
    """
    sm.tool("blit_enable", {"on": True})
    sm.tool("blit_clear")
    time.sleep(settle)
    d = json.loads(sm.tool("blit_frame", {"limit": 16384}))
    # Idle, Beta 17.1 redraws one cell and it is usually a FILL, so nothing
    # blits and there is nothing to discover. Wait a little longer before
    # resorting to input -- most screens animate something.
    for _ in range(3):
        if any(r["kind"] == 0 for r in d["draws"]):
            break
        time.sleep(settle)
        d = json.loads(sm.tool("blit_frame", {"limit": 16384}))
    if not any(r["kind"] == 0 for r in d["draws"]) and nudge:
        # Last resort, and it is a real side effect: F1 is only the commands
        # panel in the default domain. Never fatal -- the caller may well have
        # a way to proceed without the atlas.
        try:
            sm.tool("key", {"keysym": F1, "unicode": 0})
            time.sleep(0.8)
            sm.tool("key", {"keysym": F1, "unicode": 0})
            time.sleep(1.2)
            d = json.loads(sm.tool("blit_frame", {"limit": 16384}))
        except Exception:
            pass
    out = {}
    for ptr in {r["arg"] for r in d["draws"] if r["kind"] == 0}:
        got = classify(sm, ptr)
        if got:
            out[got["kind"]] = got
    if "text" in out:
        remember(out["text"])
    else:
        got = remembered(sm)
        if got:
            out["text"] = got
    return out


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".glyphcache.json")


def remembered(sm):
    """The last atlas we found, if it still looks like one.

    Discovery needs the game to be drawing, and an idle -- or paused -- game
    only fills rectangles. The pointer is stable for the life of the process,
    so remembering it across invocations makes the read work whenever the game
    happens to be sitting still, which is exactly when you want to read it.
    Validated on load, so a stale pointer from an older process is caught here
    rather than decoded into nonsense.
    """
    try:
        with open(CACHE) as f:
            got = classify(sm, json.load(f)["text"])
    except Exception:
        return None
    return got if got and got["kind"] == "text" else None


def remember(atlas):
    try:
        with open(CACHE, "w") as f:
            json.dump({"text": atlas["ptr"]}, f)
    except OSError:
        pass


def classify(sm, ptr):
    """One blit source, identified -- or None if it is not a glyph sheet.

    Sheets are 32 slots wide. A square cell is the map font, a 1:2 cell the
    text font (Cogmind's text fonts are 6x12, 7x14, 8x16 ... at 1x). Both cell
    sizes vary with the profile's font setting -- 12x24 on one profile here,
    10x20 on another -- so nothing about the geometry may be baked in.
    """
    s = surface(sm, ptr)
    if not (0 < s["w"] <= 4096 and 0 < s["h"] <= 4096):
        return None
    cw = s["w"] // SHEET_COLS
    if cw <= 0 or s["w"] % SHEET_COLS:
        return None
    for ch in (cw * 2, cw):
        if s["h"] % ch == 0:
            s.update(
                cell=(cw, ch),
                cols=SHEET_COLS,
                rows=s["h"] // ch,
                kind="text" if ch == cw * 2 else "map",
            )
            return s
    return None


# The screen surface, from a COGMIND.exe global. The image has no ASLR
# (DllCharacteristics = 0x8100, no .reloc), so this VA is literal at runtime --
# the same trick that makes the LuigiAI addresses usable. Found by scanning for
# pointers to a surface whose w/h matched the window and whose pitch was w*4:
# of the seven referrers, exactly one lived in the executable's static data.
SCREEN_SURFACE = 0x00CEFA80


def find_screen(sm):
    """The live video surface, or None if that global does not look like one.

    Reading this is the only way to see a panel that was already open before
    the harness attached: the draw log holds what CHANGED, and a panel that is
    just sitting there never changes. It costs no keypresses and is correct in
    every UI domain, which a forced repaint is not.
    """
    ptr = _words(sm, SCREEN_SURFACE, 1)[0]
    if not ptr:
        return None
    s = surface(sm, ptr)
    if not (0 < s["w"] <= 8192 and 0 < s["h"] <= 8192 and s["pixels"]):
        return None
    if s["pitch"] < s["w"] * 4:  # we decode 32bpp only
        return None
    return s


def read_video(sm, screen, atlas, tbl=None):
    """Decode the whole screen into text by matching pixels to glyph masks.

    A cell usually holds exactly two colours -- the game fills the background,
    then blits a glyph whose alpha is 0 or 255, so there is no blending to
    undo, and the glyph's pixels are the ones that are not the background.

    Everything that is not the background is therefore the glyph, which is the
    same bitmask the atlas stores -- and it has to be built that way round,
    because the shipped fonts are STROKE fonts: a glyph carries an
    anti-aliased outline, so its pixels are several colours and "equals the
    foreground colour" matches only the solid core and nothing else. Only the
    background is reliably one flat colour.

    Except where it is not: the HUD draws resource bars BEHIND its text, so a
    cell the bar's edge crosses has two backgrounds and the majority one leaves
    the bar in the mask. That silently ate every digit under a bar --
    `250/250` read as `25 /25 `. So the three commonest colours are each tried
    as the background, which costs nothing on an ordinary cell.

    Cells in the map area are drawn from the map font at twice the width, so
    their halves match nothing here and come back blank. That is the intent:
    this is for reading panels, and the map already has two better sources.
    """
    tbl = tbl or table()
    cw, ch = atlas["cell"]
    by_mask = {bits: tbl.get(slot, "\u00a4") for slot, bits in masks(sm, atlas).items()}
    raw = _bytes(sm, screen["pixels"], screen["pitch"] * screen["h"])
    pitch, out = screen["pitch"], []
    for r in range(screen["h"] // ch):
        line = []
        for c in range(screen["w"] // cw):
            x0, y0 = c * cw * 4, r * ch
            cell = b"".join(
                raw[(y0 + k) * pitch + x0 : (y0 + k) * pitch + x0 + cw * 4]
                for k in range(ch)
            )
            # Most of a roguelike screen is one flat colour; skipping those at
            # C speed is what keeps this a couple of seconds rather than ten.
            if cell.count(cell[:4]) * 4 == len(cell):
                line.append(" ")
                continue
            px = [cell[i : i + 4] for i in range(0, len(cell), 4)]
            counts = collections.Counter(px)
            hit = " "
            for bg, _ in counts.most_common(3):
                got = by_mask.get(bytes(0 if p == bg else 1 for p in px))
                if got:
                    hit = got
                    break
            line.append(hit)
        out.append("".join(line).rstrip())
    return out


def masks(sm, atlas):
    """Per slot: the alpha-channel glyph mask. This is the stable part."""
    cw, ch = atlas["cell"]
    raw = _bytes(sm, atlas["pixels"], atlas["pitch"] * atlas["h"])
    out = {}
    for r in range(atlas["rows"]):
        for c in range(atlas["cols"]):
            bits = bytes(
                1 if raw[y * atlas["pitch"] + (c * cw + k) * 4 + 3] else 0
                for y in range(r * ch, (r + 1) * ch)
                for k in range(cw)
            )
            if any(bits):
                out[r * atlas["cols"] + c] = bits
    return out


def art(bits, cw):
    """A slot's mask as ASCII, for labelling one by eye."""
    return [
        "".join("#" if b else "." for b in bits[i : i + cw])
        for i in range(0, len(bits), cw)
    ]


def table():
    """slot -> character for the shipped text font.

    Read off the alpha channel and cross-checked by decoding a live screen:
    every HUD string came out as English with these and nothing else.
    """
    t = {i: chr(32 + i) for i in range(32)}
    for i, ch in enumerate("@[\\]^_`"):
        t[32 + i] = ch
    t.update({39: "[", 41: "]", 95: "█"})
    for i in range(26):
        t[64 + i] = chr(ord("A") + i)
        t[96 + i] = chr(ord("a") + i)
    t.update({128: "│", 129: "─", 135: "├", 136: "┌", 137: "┐", 138: "┘"})
    return t


def capture(sm, atlases, repaint=True, wait=1.5):
    """The screen as {(row, col): slot}, for the text font.

    With `repaint`, open and close a panel around the clear so the game
    recomposites everything -- otherwise the log holds only the cells that
    happened to change.
    """
    sm.tool("blit_enable", {"on": True})
    if repaint:
        sm.tool("key", {"keysym": F1, "unicode": 0})
        time.sleep(0.8)
    sm.tool("blit_clear")
    time.sleep(0.2)
    if repaint:
        sm.tool("key", {"keysym": F1, "unicode": 0})
    time.sleep(wait)
    d = json.loads(sm.tool("blit_frame", {"limit": 16384}))
    if d["count"] >= 16384:
        print("warning: draw log full, the capture is truncated", file=sys.stderr)
    cw, ch = atlases["text"]["cell"]
    grid = {}
    for r in d["draws"]:
        if r["kind"] == 0 and r["arg"] == atlases["text"]["ptr"]:
            grid[(r["dy"] // ch, r["dx"] // cw)] = (r["sy"] // ch) * SHEET_COLS + r[
                "sx"
            ] // cw
    return grid, d["count"]


def render(grid, tbl):
    lines = []
    for row in sorted({p[0] for p in grid}):
        cols = [c for (y, c) in grid if y == row]
        line = "".join(
            tbl.get(grid[(row, c)], "¤") if (row, c) in grid else " "
            for c in range(max(cols) + 1)
        )
        lines.append((row, line.rstrip()))
    return lines


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=["screen", "read", "sheet", "art", "atlases"])
    ap.add_argument("slots", nargs="*", type=int, help="for `art`")
    ap.add_argument("--statmind", default=BIN)
    ap.add_argument(
        "--no-repaint",
        action="store_true",
        help="read only what changed, instead of forcing a full redraw",
    )
    a = ap.parse_args()

    sm = Statmind(a.statmind, quiet=True)
    atlases = find_atlases(sm)
    if not atlases:
        raise SystemExit("no glyph cache found -- is a map loaded and drawing?")

    if a.cmd == "atlases":
        for k, s in atlases.items():
            print(
                "%-5s 0x%08X %dx%d cell %dx%d  %dx%d slots"
                % (
                    k,
                    s["ptr"],
                    s["w"],
                    s["h"],
                    s["cell"][0],
                    s["cell"][1],
                    s["cols"],
                    s["rows"],
                )
            )
        return

    if a.cmd in ("sheet", "art"):
        m = masks(sm, atlases["text"])
        cw = atlases["text"]["cell"][0]
        if a.cmd == "art":
            for s in a.slots or sorted(m):
                print("slot %d  %r" % (s, table().get(s, "?")))
                print(
                    "\n".join("  " + line for line in art(m[s], cw))
                    if s in m
                    else "  (empty)"
                )
            return
        tbl = table()
        for r in range(atlases["text"]["rows"]):
            row = "".join(
                tbl.get(r * SHEET_COLS + c, "¤") if (r * SHEET_COLS + c) in m else " "
                for c in range(SHEET_COLS)
            )
            print("row %d |%s|" % (r, row))
        print("\n%d slots in use; ¤ = drawn but unlabelled" % len(m))
        return

    if a.cmd == "read":
        scr = find_screen(sm)
        if not scr:
            raise SystemExit("no video surface at 0x%08X" % SCREEN_SURFACE)
        t = time.time()
        lines = read_video(sm, scr, atlases["text"])
        print(
            "%dx%d screen, %dx%d cells, %.1fs\n"
            % (scr["w"], scr["h"], *atlases["text"]["cell"], time.time() - t)
        )
        for i, line in enumerate(lines):
            if line.strip():
                print("%3d|%s" % (i, line))
        return

    grid, n = capture(sm, atlases, repaint=not a.no_repaint)
    tbl = table()
    print("%d draws, %d text cells\n" % (n, len(grid)))
    for row, line in render(grid, tbl):
        if line.strip():
            print("%3d|%s" % (row, line))


if __name__ == "__main__":
    sys.exit(main() or 0)
