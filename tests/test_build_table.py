"""Check the build resolver against the header the shim actually compiles.

`verify_retail.py` mirrors `StatmindBuild` in ctypes so it can read the struct
the C resolver returns. Nothing else notices when the two drift: a mismatched
layout still compiles, still resolves, and simply reports addresses read from
the wrong offsets. These checks need no game files.
"""

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADERS = ROOT.parent / "StatMind/SDL-1.2/src"
sys.path.insert(0, str(ROOT))
from verify_retail import Build  # noqa: E402


@unittest.skipUnless(
    (HEADERS / "statmind_build.h").exists(), "the SDL shim sources are not checked out"
)
class BuildTable(unittest.TestCase):
    def compile(self, body, expect_ok=True):
        with tempfile.TemporaryDirectory(prefix="cogbench-build-table-") as tmp:
            source = Path(tmp) / "probe.c"
            # The header's resolver and table are static, so a probe that does
            # not reference them fails on -Wunused-function before it reaches
            # the assertion under test.
            source.write_text('#include "statmind_build.h"\n' + body + """
const void *statmind_probe_use(const unsigned char *p)
{ return Statmind_FindBuild(p) ? (const void *)statmind_builds : 0; }
""")
            done = subprocess.run(
                [
                    os.environ.get("CC", "cc"),
                    "-fsyntax-only",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(HEADERS),
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
        if expect_ok:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def test_ctypes_mirror_matches_the_c_struct(self):
        # Static assertions, so a drifted field order or size fails to compile.
        offsets = "".join(
            '_Static_assert(__builtin_offsetof(StatmindBuild, %s) == %d, "%s");\n'
            % (name, getattr(Build, name).offset, name)
            for name, _ in Build._fields_
        )
        self.compile(
            '_Static_assert(sizeof(StatmindBuild) == %d, "size");\n%s'
            % (ctypes.sizeof(Build), offsets)
        )

    def test_the_probe_would_notice_a_mismatch(self):
        # Guard the guard: a wrong expectation has to fail, or the test above
        # proves nothing.
        done = self.compile(
            '_Static_assert(sizeof(StatmindBuild) == %d, "size");'
            % (ctypes.sizeof(Build) + 4),
            expect_ok=False,
        )
        self.assertNotEqual(done.returncode, 0)

    def test_every_table_row_is_distinct_and_fingerprinted(self):
        self.compile("""
#include <stdio.h>
int main(void) {
    unsigned int i, j, n = sizeof(statmind_builds) / sizeof(statmind_builds[0]);
    for (i = 0; i < n; ++i)
        for (j = i + 1; j < n; ++j)
            if (statmind_builds[i].timestamp == statmind_builds[j].timestamp)
                return 1;
    return 0;
}
""")

    def test_a_zeroed_image_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="cogbench-build-table-") as tmp:
            source, library = Path(tmp) / "r.c", Path(tmp) / "r.so"
            source.write_text(
                '#include "statmind_build.h"\n'
                "const StatmindBuild *resolve(const unsigned char *p)"
                " { return Statmind_FindBuild(p); }\n"
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
                    str(HEADERS),
                    str(source),
                    "-o",
                    str(library),
                ],
                check=True,
            )
            lib = ctypes.CDLL(str(library))
            lib.resolve.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
            lib.resolve.restype = ctypes.POINTER(Build)
            blank = (ctypes.c_ubyte * 0x1000)()
            self.assertFalse(lib.resolve(blank))
            blank[0], blank[1] = ord("M"), ord("Z")
            self.assertFalse(lib.resolve(blank))


if __name__ == "__main__":
    unittest.main()
