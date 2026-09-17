#!/usr/bin/env python3
"""Verify owned executables against the SDL shim's actual C build resolver.

No game is launched. Compile the portable resolver with the host C compiler,
map PE sections, and check acceptance plus rejection of damaged fingerprints.

The resolver covers every address the shim itself uses, each pinned to an
instruction. It cannot cover the four addresses StatMind reads from outside the
process, because those are plain data with no code to fingerprint. Those are
checked here instead, against the file on disk, where .data still holds what
the linker put there rather than what the running game has since written.

Usage: python3 verify_retail.py /path/to/COGMIND.exe [another/COGMIND.exe]
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


class Build(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in (
            "timestamp",
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
    ]
    _fields_.append(("writer_sig", ctypes.c_ubyte * 10))


# Fixed VAs StatMind reads over the process boundary (StatMind/src/cells.rs,
# src/blit.rs). The image has no .reloc and no DYNAMIC_BASE, so it always loads
# at 0x00400000 and these are literal at runtime.
DATA_SECTION = {"rva": 0x8A8000, "vsize": 0x943FC}
MAP_OBJ = 0x00CFD44C  # { int width; int height; Cell **cells; }
VIEW_ORIGIN = 0x00CD8FA4  # { int x; int y; } -- linker initialiser is (27, 8)
PLAYER_REC = 0x00D2D338  # { u32 handle; i32 x; i32 y; i32 entity_id }


def map_pe(data):
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("not a PE executable")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe > 0x1000 or data[pe : pe + 6] != b"PE\0\0L\x01":
        raise ValueError("not a supported i386 PE")
    count = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    size, headers = struct.unpack_from("<II", data, pe + 24 + 56)
    if size != 0x940000 or not 0 < headers <= min(size, len(data)):
        raise ValueError("unexpected image/header size")
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


def verify_data(image, sections):
    """Check the fixed .data addresses StatMind reads over the process boundary.

    These carry no instruction to fingerprint, so the evidence is the layout
    itself: an identical .data start and virtual size means the linker placed
    the whole section, zero-fill tail included, exactly where it was before.
    That is what fixes the three zero-fill addresses; the view origin sits in
    initialised data and can be checked against its initialiser outright.
    """
    data = sections.get(".data")
    if data != DATA_SECTION:
        raise ValueError(
            ".data moved or resized: %r, expected %r -- every fixed"
            " address below is unsafe on this build" % (data, DATA_SECTION)
        )
    origin = struct.unpack_from("<ii", image, VIEW_ORIGIN - 0x400000)
    if origin != (27, 8):
        raise ValueError("view origin initialiser is %r, expected (27, 8)" % (origin,))
    text = sections[".text"]
    span = bytes(image[text["rva"] : text["rva"] + text["vsize"]])
    return {
        "data_section": "rva 0x%X, vsize 0x%X, as expected"
        % (data["rva"], data["vsize"]),
        "map_object": "0x%08X, this in %d thiscall sites"
        % (MAP_OBJ, span.count(struct.pack("<BI", 0xB9, MAP_OBJ))),
        "view_origin": "0x%08X, initialiser (27, 8)" % VIEW_ORIGIN,
        "player_record": "0x%08X, zero-fill with no static reference"
        " -- confirm at runtime with snapshot.sh" % PLAYER_REC,
    }


def verify(path, resolve):
    data = path.read_bytes()
    image, pe, sections = map_pe(data)
    view = (ctypes.c_ubyte * len(image)).from_buffer(image)
    result = resolve(view)
    if not result:
        raise ValueError(f"{path}: unsupported build or fingerprint mismatch")
    build = result.contents
    # Every trust anchor, including the relative call and singleton, must fail
    # closed when corrupted. Exercise the production C resolver, not a copy.
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
        build.callsite,
        build.callsite + 7,
        build.callsite + 11,
        build.callsite + 12,
        build.luigi_gate,
        build.cell_at,
        build.cell_at + 14,
        build.map_callsite,
        build.map_callsite + 1,
        build.map_callsite + 6,
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
        "addresses": {
            name: hex(0x400000 + getattr(build, name))
            for name, _ in Build._fields_[1:-1]
        },
        "data_addresses": verify_data(image, sections),
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
