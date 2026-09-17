#!/usr/bin/env python3
"""Verify owned Cogmind executables against the SDL shim's actual C resolver.

No game is launched. Compile the resolver from the header the shim really
compiles, map PE sections, and check both that each build is accepted and that
every trust anchor in it fails closed when a single bit is flipped.

The resolver covers everything reachable from an instruction. Two addresses are
not: the view origin, which at least has an initialiser to check, and the player
record, which is zero-fill reached through a pointer and can only be settled by
reading a running game. Those are reported here rather than proved.

Usage: python3 verify_retail.py /path/to/COGMIND.exe [more...]
"""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile

IMAGE_BASE = 0x400000

# .text RVAs; each is pinned to an instruction by the resolver.
CODE_FIELDS = (
    "init",
    "writer",
    "dump_bool",
    "history_guard",
    "epilogue",
    "callsite",
    "luigi_gate",
    "cell_at",
    "map_callsite",
)
# Absolute VAs of the data this build uses. The first four are encoded in the
# instructions above, so a fingerprint match confirms them; the last three are
# not, and luigi_test may legitimately be unknown.
DATA_FIELDS = (
    "active",
    "luigi",
    "map_object",
    "scorekeeper",
    "view_origin",
    "player_rec",
    "luigi_test",
)


class Build(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in ("timestamp", "image_size") + CODE_FIELDS + DATA_FIELDS
    ]
    _fields_.append(("writer_sig", ctypes.c_ubyte * 10))


def map_pe(data):
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("not a PE executable")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe > 0x1000 or data[pe : pe + 6] != b"PE\0\0L\x01":
        raise ValueError("not a supported i386 PE")
    count = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    size, headers = struct.unpack_from("<II", data, pe + 24 + 56)
    # Size is not fixed across builds -- the Steam executable is a page larger
    # than the others -- so bound it rather than pinning it.
    if not 0x800000 <= size <= 0x1000000 or not 0 < headers <= min(size, len(data)):
        raise ValueError(
            "unexpected image size 0x%X or header size 0x%X" % (size, headers)
        )
    image = bytearray(size)
    image[:headers] = data[:headers]
    sections = {}
    for n in range(count):
        section = pe + 24 + opt_size + 40 * n
        name = data[section : section + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, rva, raw_size, raw = struct.unpack_from("<IIII", data, section + 8)
        if rva + raw_size > size or raw + raw_size > len(data):
            raise ValueError("section outside image or file")
        image[rva : rva + raw_size] = data[raw : raw + raw_size]
        sections[name] = {"rva": rva, "vsize": vsize}
    return image, pe, sections


def verify_data(build, image, sections):
    """Report on the data addresses, and check the ones that can be checked.

    Four of these are encoded in instructions the resolver already matched, so
    reaching here means they are confirmed. The view origin is initialised data
    and is checked against its initialiser. The player record is zero-fill with
    no reference anywhere in .text, so there is nothing to check it against.
    """
    data = sections.get(".data")
    if not data:
        raise ValueError("no .data section")
    lo = IMAGE_BASE + data["rva"]
    hi = lo + data["vsize"]
    out = {"data_section": "rva 0x%X, vsize 0x%X" % (data["rva"], data["vsize"])}

    for name in ("active", "luigi", "map_object", "scorekeeper"):
        va = getattr(build, name)
        if not lo <= va < hi:
            raise ValueError("%s 0x%08X is outside .data" % (name, va))
        out[name] = "0x%08X, pinned by instruction" % va

    origin = build.view_origin
    if not lo <= origin < hi:
        raise ValueError("view origin 0x%08X is outside .data" % origin)
    pair = struct.unpack_from("<ii", image, origin - IMAGE_BASE)
    if pair != (27, 8):
        raise ValueError(
            "view origin 0x%08X initialiser is %r, expected (27, 8)" % (origin, pair)
        )
    out["view_origin"] = "0x%08X, initialiser (27, 8)" % origin

    if build.player_rec:
        if not lo <= build.player_rec < hi:
            raise ValueError("player record 0x%08X is outside .data" % build.player_rec)
        out["player_rec"] = (
            "0x%08X, zero-fill with no static reference"
            " -- confirmed only by a live reading" % build.player_rec
        )
    else:
        out["player_rec"] = (
            "not located on this build; the reader refuses player queries"
        )
    out["luigi_test"] = (
        ("0x%08X" % build.luigi_test) if build.luigi_test else "not identified"
    )
    return out


def verify(path, resolve):
    data = path.read_bytes()
    image, pe, sections = map_pe(data)
    view = (ctypes.c_ubyte * len(image)).from_buffer(image)
    result = resolve(view)
    if not result:
        raise ValueError(f"{path}: unsupported build or fingerprint mismatch")
    build = result.contents
    # Every trust anchor, including each address embedded in an instruction and
    # the relative calls, must fail closed when corrupted. This exercises the
    # production C resolver, not a copy of it.
    mutations = [
        0,
        pe,
        pe + 4,
        pe + 8,
        pe + 24,
        pe + 24 + 28,
        pe + 24 + 56,
        pe + 24 + 70,
        build.init + 10,
        build.init + 19,
        build.writer,
        build.dump_bool,
        build.history_guard,
        build.epilogue,
        build.luigi_gate,
        build.luigi_gate + 3,
        build.luigi_gate + 12,
        build.cell_at,
        build.cell_at + 14,
        build.map_callsite,
        build.map_callsite + 1,
        build.map_callsite + 6,
        build.callsite,
        build.callsite + 7,
        build.callsite + 11,
        build.callsite + 12,
    ]
    for offset in mutations:
        image[offset] ^= 0x40
        if resolve(view):
            raise AssertionError(f"accepted corrupt fingerprint at RVA {offset:#x}")
        image[offset] ^= 0x40
    return {
        "executable": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "timestamp": hex(build.timestamp),
        "image_size": hex(build.image_size),
        "code_addresses": {
            name: hex(IMAGE_BASE + getattr(build, name)) for name in CODE_FIELDS
        },
        "data_addresses": verify_data(build, image, sections),
        "rejection_checks": len(mutations),
        "static_verification": "passed",
        "runtime_verification": "not performed by this tool",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executables", nargs="+", type=Path)
    args = parser.parse_args()
    headers = Path(__file__).resolve().parent.parent / "StatMind/SDL-1.2/src"
    with tempfile.TemporaryDirectory(prefix="cogbench-verify-") as tmp:
        source = Path(tmp) / "verify.c"
        library = Path(tmp) / "verify.so"
        source.write_text(
            '#include "statmind_build.h"\n'
            "const StatmindBuild *resolve(const unsigned char *p) "
            "{ return Statmind_FindBuild(p); }\n"
        )
        subprocess.run(
            [
                os.environ.get("CC", "cc"),
                "-shared",
                "-fPIC",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(headers),
                str(source),
                "-o",
                str(library),
            ],
            check=True,
        )
        lib = ctypes.CDLL(str(library))
        lib.resolve.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        lib.resolve.restype = ctypes.POINTER(Build)
        results = []
        for path in args.executables:
            try:
                results.append(verify(path, lib.resolve))
            except ValueError as exc:
                raise SystemExit("verification failed: %s" % exc)
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
