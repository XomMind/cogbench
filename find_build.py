#!/usr/bin/env python3
"""Locate every address a new Cogmind build needs, and emit its table row.

Adding a build to `statmind_build.h` means finding nine instruction anchors and
seven addresses. This finds them by fingerprint rather than by rebasing a known
build, because Cogmind's builds do not move by a constant: between the two
Beta 17.1 executables the code shifted by five different amounts in both
directions, and the Steam build moved .data as well.

Two addresses have no instruction to find them by, so they are recovered by
comparison with a build that already has them (`--reference`):

  view origin   matched on the bytes surrounding it in initialised .data
  player record located relative to the neighbouring global, which is itself
                matched by the bodies of the methods called on it -- method
                bodies are byte-identical across builds even when addresses
                are not. This one is a CANDIDATE and must be confirmed against
                a running game before it goes in the table.

Usage:
  python3 find_build.py NEW.exe [--reference KNOWN.exe]
  python3 find_build.py --self-test KNOWN.exe   # reproduce a shipped row
"""

import argparse
import hashlib
import re
import struct
from collections import Counter, defaultdict
from pathlib import Path

IMAGE_BASE = 0x400000
# cellAt: this->cells[x * this->height + y]. Byte-identical in every build so
# far, which is what makes it usable as an anchor at all.
CELL_AT = bytes.fromhex(
    "558bec51894dfc8b45fc8b4d080faf4804034d0c8b55fc8b42088d04888be55dc20800"
)


class Image:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        d = self.data
        self.pe = struct.unpack_from("<I", d, 0x3C)[0]
        if d[self.pe : self.pe + 6] != b"PE\0\0L\x01":
            raise SystemExit("%s: not a supported i386 PE" % path)
        nsec = struct.unpack_from("<H", d, self.pe + 6)[0]
        opt = struct.unpack_from("<H", d, self.pe + 20)[0]
        self.timestamp = struct.unpack_from("<I", d, self.pe + 8)[0]
        self.image_size = struct.unpack_from("<I", d, self.pe + 24 + 56)[0]
        hdr = struct.unpack_from("<I", d, self.pe + 24 + 60)[0]
        img = bytearray(self.image_size)
        img[:hdr] = d[:hdr]
        self.sections = {}
        for n in range(nsec):
            off = self.pe + 24 + opt + 40 * n
            name = d[off : off + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, rva, rawsz, raw = struct.unpack_from("<IIII", d, off + 8)
            img[rva : rva + rawsz] = d[raw : raw + rawsz]
            self.sections[name] = {"rva": rva, "vsize": vsize, "rawsz": rawsz}
        self.image = bytes(img)
        t = self.sections[".text"]
        self.tlo, self.thi = t["rva"], t["rva"] + t["rawsz"]

    def va(self, rva):
        return IMAGE_BASE + rva

    def find(self, needle, start=None):
        return self.image.find(needle, self.tlo if start is None else start, self.thi)

    def calls_to(self, target):
        out = []
        for i in range(self.tlo, self.thi - 4):
            if self.image[i] == 0xE8:
                rel = struct.unpack_from("<i", self.image, i + 1)[0]
                if i + 5 + rel == target:
                    out.append(i)
        return out

    def thiscall_profile(self):
        """Fingerprint each global used as `this` by the methods called on it."""
        d = self.sections[".data"]
        lo, hi = self.va(d["rva"]), self.va(d["rva"]) + d["vsize"]
        prof = defaultdict(Counter)
        for m in re.finditer(
            rb"\xb9(....)\xe8(....)", self.image[self.tlo : self.thi], re.S
        ):
            at = self.tlo + m.start()
            g = struct.unpack("<I", m.group(1))[0]
            if not lo <= g < hi:
                continue
            tgt = at + 10 + struct.unpack("<i", m.group(2))[0]
            prof[g][
                hashlib.blake2b(self.image[tgt : tgt + 32], digest_size=8).digest()
            ] += 1
        return prof


def anchors(img):
    """The nine instruction anchors and the four addresses they encode."""
    init = img.find(bytes.fromhex("c7004cfaad64")) - 10
    if init < 0 or img.image[init + 19 : init + 26] != bytes.fromhex("c74104d93e5379"):
        raise SystemExit("%s: LuigiAi::initialize not found" % img.path)

    # The LuigiAi instance is whatever `this` initialize's callers load.
    ecx = Counter()
    for c in img.calls_to(init):
        for back in range(5, 24):
            if img.image[c - back] == 0xB9:
                ecx[struct.unpack_from("<I", img.image, c - back + 1)[0]] += 1
                break
    luigi = ecx.most_common(1)[0][0]

    # The gate is the only test of a byte that guards a call on that instance.
    pat = re.compile(
        rb"\x0f\xb6\x05(....)\x85\xc0\x74\x0a\xb9"
        + re.escape(struct.pack("<I", luigi)),
        re.S,
    )
    gates = [
        (img.tlo + m.start(), struct.unpack("<I", m.group(1))[0])
        for m in pat.finditer(img.image[img.tlo : img.thi])
    ]
    if len(gates) != 1:
        raise SystemExit(
            "%s: expected one LuigiAI gate, found %d" % (img.path, len(gates))
        )
    gate, active = gates[0]

    cell_at = img.find(CELL_AT)
    sites = []
    for m in re.finditer(rb"\xb9(....)\xe8(....)", img.image[img.tlo : img.thi], re.S):
        at = img.tlo + m.start()
        if at + 10 + struct.unpack("<i", m.group(2))[0] == cell_at:
            sites.append((at, struct.unpack("<I", m.group(1))[0]))
    # Several objects call cellAt; the map singleton is the overwhelming majority.
    map_object, uses = Counter(o for _, o in sites).most_common(1)[0]
    map_callsite = next(a for a, o in sites if o == map_object)

    cs = next(
        i
        for i in range(img.tlo, img.thi)
        if img.image[i : i + 7] == bytes.fromhex("6a018d45d450b9")
        and img.image[i + 11] == 0xE8
    )
    scorekeeper = struct.unpack_from("<I", img.image, cs + 7)[0]
    writer = cs + 16 + struct.unpack_from("<i", img.image, cs + 12)[0]
    if img.image[writer : writer + 5] != bytes.fromhex("558bec6aff"):
        raise SystemExit("%s: writer prologue mismatch" % img.path)

    return {
        "timestamp": img.timestamp,
        "image_size": img.image_size,
        "init": init,
        "writer": writer,
        "dump_bool": img.find(bytes.fromhex("0fb6450c85c00f85"), writer),
        "history_guard": img.find(bytes.fromhex("0fb64d0c85c9750b")),
        "epilogue": img.find(bytes.fromhex("8be55dc20800"), writer),
        "callsite": cs,
        "luigi_gate": gate,
        "cell_at": cell_at,
        "map_callsite": map_callsite,
        "active": active,
        "luigi": luigi,
        "map_object": map_object,
        "scorekeeper": scorekeeper,
        "map_uses": uses,
        "writer_sig": img.image[writer : writer + 10],
    }


def view_origin(new, ref, ref_va, context=32):
    """Match the initialised bytes surrounding a known view origin."""
    blob = ref.image[ref_va - IMAGE_BASE - context : ref_va - IMAGE_BASE + context]
    d = new.sections[".data"]
    lo, hi = d["rva"], d["rva"] + d["rawsz"]
    hits, i = [], lo
    while True:
        j = new.image.find(blob, i, hi)
        if j < 0:
            break
        hits.append(j + context)
        i = j + 1
    if len(hits) != 1:
        return None, "%d matches for the surrounding %d bytes" % (
            len(hits),
            context * 2,
        )
    va = new.va(hits[0])
    pair = struct.unpack_from("<ii", new.image, hits[0])
    if pair != (27, 8):
        return None, "candidate 0x%08X holds %r, not (27, 8)" % (va, pair)
    return va, "matched on %d bytes of context, initialiser (27, 8)" % (context * 2)


def player_rec(new, ref, ref_va, ref_neighbour):
    """Locate the record relative to the neighbouring global below it."""
    pr, pn = ref.thiscall_profile(), new.thiscall_profile()
    want = pr.get(ref_neighbour)
    if not want:
        return None, "the reference neighbour has no thiscall profile"

    def overlap(a, b):
        ka, kb = set(a), set(b)
        return len(ka & kb) / len(ka | kb) if ka | kb else 0

    ranked = sorted(((overlap(want, p), g) for g, p in pn.items()), reverse=True)
    score, best = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else 0
    if score <= 0 or score <= runner * 1.5:
        return None, "no clear match for the neighbouring global (%.3f vs %.3f)" % (
            score,
            runner,
        )
    return best + (ref_va - ref_neighbour), (
        "CANDIDATE: neighbour matched at %.3f (next best %.3f), record taken at "
        "the same +0x%X offset. Confirm on a running game before use."
        % (score, runner, ref_va - ref_neighbour)
    )


def row(a, view, player):
    sig = ", ".join("0x%02X" % b for b in a["writer_sig"])
    return (
        "    { 0x%08X, 0x%X,\n"
        "      0x%X, 0x%X, 0x%X, 0x%X, 0x%X, 0x%X, 0x%X,\n"
        "      0x%X, 0x%X,\n"
        "      0x%08X, 0x%08X, 0x%08X, 0x%08X,\n"
        "      0x%08X, 0x%08X,\n"
        "      0x%08X,\n"
        "      { %s } }"
        % (
            a["timestamp"],
            a["image_size"],
            a["init"],
            a["writer"],
            a["dump_bool"],
            a["history_guard"],
            a["epilogue"],
            a["callsite"],
            a["luigi_gate"],
            a["cell_at"],
            a["map_callsite"],
            a["active"],
            a["luigi"],
            a["map_object"],
            a["scorekeeper"],
            view or 0,
            player or 0,
            0,
            sig,
        )
    )


# The shipped row for the first Beta 17.1, used by --self-test.
KNOWN = {
    0x6A8CFC58: dict(
        init=0x34C00,
        writer=0x74B90,
        dump_bool=0x74BD0,
        history_guard=0x7E920,
        epilogue=0x7F43D,
        callsite=0x3D640B,
        luigi_gate=0x30E358,
        cell_at=0x5CF7D0,
        map_callsite=0x84CD7,
        active=0x00CEFB3E,
        luigi=0x00CEBFFC,
        map_object=0x00CFD44C,
        scorekeeper=0x00D2C658,
    ),
    0x6A9CBFDF: dict(
        init=0x34CD0,
        writer=0x74A20,
        dump_bool=0x74A60,
        history_guard=0x7E7B0,
        epilogue=0x7F2CD,
        callsite=0x3D66BB,
        luigi_gate=0x30E338,
        cell_at=0x5CEDA0,
        map_callsite=0x84B67,
        active=0x00CEFB3E,
        luigi=0x00CEBFFC,
        map_object=0x00CFD44C,
        scorekeeper=0x00D2C658,
    ),
}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("executable", type=Path)
    ap.add_argument(
        "--reference",
        type=Path,
        help="a build whose view origin and player record are known",
    )
    ap.add_argument("--ref-view-origin", type=lambda s: int(s, 0), default=0x00CD8FA4)
    ap.add_argument("--ref-player-rec", type=lambda s: int(s, 0), default=0x00D2D338)
    ap.add_argument(
        "--ref-player-neighbour", type=lambda s: int(s, 0), default=0x00D2D2A0
    )
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="check the result against the shipped table row",
    )
    args = ap.parse_args()

    img = Image(args.executable)
    a = anchors(img)
    print("%s" % args.executable)
    print("  sha256     %s" % hashlib.sha256(img.data).hexdigest())
    print(
        "  timestamp  0x%08X   image size 0x%X   bytes %d"
        % (a["timestamp"], a["image_size"], len(img.data))
    )
    d = img.sections[".data"]
    print("  .data      rva 0x%X vsize 0x%X" % (d["rva"], d["vsize"]))
    print("  map singleton used by %d cellAt call sites" % a["map_uses"])

    if args.self_test:
        want = KNOWN.get(a["timestamp"])
        if not want:
            raise SystemExit("no shipped row for timestamp 0x%08X" % a["timestamp"])
        bad = {k: (a[k], v) for k, v in want.items() if a[k] != v}
        if bad:
            for k, (got, exp) in bad.items():
                print("  MISMATCH %-14s found 0x%X expected 0x%X" % (k, got, exp))
            raise SystemExit(1)
        print("  self-test PASSED: every anchor matches the shipped row")
        return

    view, view_why = (None, "no --reference given")
    player, player_why = (None, "no --reference given")
    if args.reference:
        ref = Image(args.reference)
        view, view_why = view_origin(img, ref, args.ref_view_origin)
        player, player_why = player_rec(
            img, ref, args.ref_player_rec, args.ref_player_neighbour
        )
    print(
        "  view origin   %s  -- %s"
        % ("0x%08X" % view if view else "NOT FOUND", view_why)
    )
    print(
        "  player record %s  -- %s"
        % ("0x%08X" % player if player else "NOT FOUND", player_why)
    )
    print("\nTable row for statmind_build.h (player record left 0 until confirmed):\n")
    print(row(a, view, None))


if __name__ == "__main__":
    main()
