#!/usr/bin/env python3
"""
Recover the b17.1 scoresheet schema from COGMIND.exe.

Why this exists
---------------
Cogmind links protobuf 3.5.1 and compiles `src/web/scoresheet.pb.cc`, so the
binary carries the *serialized FileDescriptorProto* for `web/scoresheet.proto`
verbatim in .rdata -- that is simply how protobuf's generated code registers a
schema at startup. Reading it back gives the exact schema for the build you are
running, which beats the public prerelease repo (a snapshot of some other build)
and beats inferring fields from a JSON sample (JSON omits defaults, so absent
fields are invisible).

Nothing here is decompilation: the descriptor is data, and this only parses it.

The parse is hand-rolled because the `protobuf` Python package is not a
dependency of this harness. It covers the subset of descriptor.proto that
protoc actually emits for a proto3 file. `--verify` cross-checks the extracted
blob against `protoc --decode`, which is the real proof the byte range is exact.
"""

import argparse
import os
import subprocess
import sys

DEFAULT_EXE = (
    "/Applications/Cogmind.app/Contents/SharedSupport/prefix/drive_c/"
    "COGMIND (Beta 17.1)/COGMIND.exe"
)

# The FileDescriptorProto always begins with field 1 (name), wire type 2, so the
# first two bytes are 0x0A then the name length. Anchoring on both means we find
# the descriptor itself and not one of the assert-path strings that mention it.
ANCHOR = b"\x0a\x14web/scoresheet.proto"

TYPES = {
    1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32",
    6: "fixed64", 7: "fixed32", 8: "bool", 9: "string", 10: "group",
    11: "message", 12: "bytes", 13: "uint32", 14: "enum",
    15: "sfixed32", 16: "sfixed64", 17: "sint32", 18: "sint64",
}
LABELS = {1: "optional", 2: "required", 3: "repeated"}


# ---------------------------------------------------------------- wire parsing

def varint(buf, i):
    r = s = 0
    while True:
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << s
        s += 7
        if not b & 0x80:
            return r, i


def fields(buf):
    """Yield (field_number, value) for one message. Values are int or bytes."""
    i, n = 0, len(buf)
    while i < n:
        tag, i = varint(buf, i)
        num, wire = tag >> 3, tag & 7
        if wire == 0:
            val, i = varint(buf, i)
        elif wire == 2:
            ln, i = varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            val, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        elif wire == 1:
            val, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        else:
            raise ValueError("wire type %d (group?) at %d" % (wire, i))
        yield num, val


def find_blob(data):
    """Locate the descriptor and return (start, end) as file offsets.

    The length is not stored next to the blob -- it is an immediate in the
    registration call -- so the end is found by walking top-level fields until
    the bytes stop being a valid FileDescriptorProto. That terminates on the
    first byte of whatever .rdata holds next, which is why the walk rejects
    implausible field numbers and lengths rather than only malformed varints.
    """
    start = data.find(ANCHOR)
    if start < 0:
        raise SystemExit("descriptor anchor not found -- wrong build?")
    if data.find(ANCHOR, start + 1) >= 0:
        print("note: anchor appears more than once; using the first", file=sys.stderr)

    i = start
    while i < len(data):
        j = i
        try:
            tag, j = varint(data, j)
        except IndexError:
            break
        num, wire = tag >> 3, tag & 7
        # FileDescriptorProto tops out at field 12 (syntax).
        if num == 0 or num > 12 or wire not in (0, 1, 2, 5):
            break
        if wire == 2:
            ln, j = varint(data, j)
            if ln > len(data) - j:
                break
            j += ln
        elif wire == 0:
            _, j = varint(data, j)
        elif wire == 5:
            j += 4
        else:
            j += 8
        i = j
    return start, i


# ------------------------------------------------------------------ descriptor

class Field:
    def __init__(self, buf):
        self.name = self.type_name = self.json_name = self.default = None
        self.number = self.label = self.type = None
        self.oneof = None
        self.packed = None
        for num, val in fields(buf):
            if num == 1:
                self.name = val.decode()
            elif num == 3:
                self.number = val
            elif num == 4:
                self.label = val
            elif num == 5:
                self.type = val
            elif num == 6:
                self.type_name = val.decode()
            elif num == 7:
                self.default = val.decode()
            elif num == 8:  # FieldOptions
                for o, ov in fields(val):
                    if o == 2:
                        self.packed = bool(ov)
            elif num == 9:
                self.oneof = val
            elif num == 10:
                self.json_name = val.decode()

    def type_str(self):
        if self.type in (11, 14):
            return self.type_name
        return TYPES.get(self.type, "?%s" % self.type)


class Enum:
    def __init__(self, buf):
        self.name = None
        self.values = []
        for num, val in fields(buf):
            if num == 1:
                self.name = val.decode()
            elif num == 2:
                vn, vv = None, 0
                for k, v in fields(val):
                    if k == 1:
                        vn = v.decode()
                    elif k == 2:
                        # Enum values are signed; protobuf writes negatives as
                        # 10-byte two's-complement varints, not zigzag.
                        vv = v - (1 << 64) if v >= (1 << 63) else v
                self.values.append((vn, vv))


class Message:
    def __init__(self, buf):
        self.name = None
        self.fields = []
        self.nested = []
        self.enums = []
        self.oneofs = []
        self.map_entry = False
        for num, val in fields(buf):
            if num == 1:
                self.name = val.decode()
            elif num == 2:
                self.fields.append(Field(val))
            elif num == 3:
                self.nested.append(Message(val))
            elif num == 4:
                self.enums.append(Enum(val))
            elif num == 7:  # MessageOptions
                for o, ov in fields(val):
                    if o == 7:
                        self.map_entry = bool(ov)
            elif num == 8:
                for k, v in fields(val):
                    if k == 1:
                        self.oneofs.append(v.decode())


class File:
    def __init__(self, buf):
        self.name = self.package = self.syntax = None
        self.deps = []
        self.messages = []
        self.enums = []
        self.services = []
        for num, val in fields(buf):
            if num == 1:
                self.name = val.decode()
            elif num == 2:
                self.package = val.decode()
            elif num == 3:
                self.deps.append(val.decode())
            elif num == 4:
                self.messages.append(Message(val))
            elif num == 5:
                self.enums.append(Enum(val))
            elif num == 6:
                svc = {"name": None, "methods": []}
                for k, v in fields(val):
                    if k == 1:
                        svc["name"] = v.decode()
                    elif k == 2:
                        m = {"name": None, "in": None, "out": None}
                        for mk, mv in fields(v):
                            if mk == 1:
                                m["name"] = mv.decode()
                            elif mk == 2:
                                m["in"] = mv.decode()
                            elif mk == 3:
                                m["out"] = mv.decode()
                        svc["methods"].append(m)
                self.services.append(svc)
            elif num == 12:
                self.syntax = val.decode()


# --------------------------------------------------------------- .proto output

def emit(f, out):
    w = out.write
    w('syntax = "%s";\n\n' % (f.syntax or "proto2"))
    if f.package:
        w("package %s;\n\n" % f.package)
    for d in f.deps:
        w('import "%s";\n' % d)
    if f.deps:
        w("\n")
    for e in f.enums:
        emit_enum(e, w, 0)
    for m in f.messages:
        emit_msg(m, w, 0, f.package)
    for s in f.services:
        w("service %s {\n" % s["name"])
        for m in s["methods"]:
            w("  rpc %s (%s) returns (%s);\n" % (m["name"], m["in"], m["out"]))
        w("}\n\n")


def emit_enum(e, w, ind):
    p = "  " * ind
    w("%senum %s {\n" % (p, e.name))
    for n, v in e.values:
        w("%s  %s = %d;\n" % (p, n, v))
    w("%s}\n\n" % p)


def emit_msg(m, w, ind, package):
    # protoc synthesises a nested XxxEntry message for every map field. Those
    # are an implementation detail of the wire format, so they are folded back
    # into `map<k, v>` here and not printed as messages of their own.
    maps = {n.name: n for n in m.nested if n.map_entry}
    p = "  " * ind
    w("%smessage %s {\n" % (p, m.name))
    for n in m.nested:
        if n.name not in maps:
            emit_msg(n, w, ind + 1, package)
    for e in m.enums:
        emit_enum(e, w, ind + 1)

    printed_oneof = set()
    for fd in m.fields:
        if fd.oneof is not None and fd.oneof not in printed_oneof:
            printed_oneof.add(fd.oneof)
            w("%s  oneof %s {\n" % (p, m.oneofs[fd.oneof]))
            for g in m.fields:
                if g.oneof == fd.oneof:
                    w("%s    %s %s = %d;\n" % (p, g.type_str(), g.name, g.number))
            w("%s  }\n" % p)
        elif fd.oneof is not None:
            continue
        else:
            entry = maps.get(short(fd.type_name)) if fd.type == 11 else None
            if entry:
                k = next(x for x in entry.fields if x.number == 1)
                v = next(x for x in entry.fields if x.number == 2)
                w("%s  map<%s, %s> %s = %d;\n"
                  % (p, k.type_str(), v.type_str(), fd.name, fd.number))
            else:
                lab = "repeated " if fd.label == 3 else ""
                w("%s  %s%s %s = %d;\n"
                  % (p, lab, fd.type_str(), fd.name, fd.number))
    w("%s}\n\n" % p)


def short(type_name):
    return type_name.rsplit(".", 1)[-1] if type_name else None


# ------------------------------------------------------------------------ main

def summarise(f, out):
    out.write("file      %s\n" % f.name)
    out.write("package   %s\n" % f.package)
    out.write("syntax    %s\n" % f.syntax)
    out.write("messages  %d\nenums     %d\nservices  %d\n"
              % (len(f.messages), len(f.enums), len(f.services)))
    out.write("\ntop-level messages (fields):\n")
    for m in f.messages:
        out.write("  %-32s %d\n" % (m.name, len(m.fields)))
    out.write("\ntop-level enums (values):\n")
    for e in f.enums:
        out.write("  %-32s %d\n" % (e.name, len(e.values)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exe", default=DEFAULT_EXE)
    ap.add_argument("-o", "--out", help="write .proto here (default stdout)")
    ap.add_argument("--raw", help="also write the raw descriptor blob here")
    ap.add_argument("--summary", action="store_true",
                    help="print a schema summary instead of .proto source")
    ap.add_argument("--verify", action="store_true",
                    help="cross-check the blob with protoc --decode")
    a = ap.parse_args()

    data = open(a.exe, "rb").read()
    start, end = find_blob(data)
    blob = data[start:end]
    print("descriptor: file offset 0x%x, %d bytes" % (start, len(blob)),
          file=sys.stderr)

    if a.raw:
        open(a.raw, "wb").write(blob)

    if a.verify:
        inc = "/opt/homebrew/opt/protobuf/include"
        if not os.path.isdir(inc):
            inc = subprocess.run(["brew", "--prefix", "protobuf"],
                                 capture_output=True, text=True
                                 ).stdout.strip() + "/include"
        r = subprocess.run(
            ["protoc", "-I" + inc,
             "--decode=google.protobuf.FileDescriptorProto",
             "google/protobuf/descriptor.proto"],
            input=blob, capture_output=True)
        if r.returncode != 0:
            raise SystemExit("protoc rejected the blob:\n"
                             + r.stderr.decode()[:2000])
        print("verify: protoc decoded %d bytes -> %d lines of text"
              % (len(blob), r.stdout.count(b"\n")), file=sys.stderr)

    f = File(blob)
    out = open(a.out, "w") if a.out else sys.stdout
    try:
        (summarise if a.summary else emit)(f, out)
    finally:
        if a.out:
            out.close()


if __name__ == "__main__":
    main()
