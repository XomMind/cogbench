# Three Beta 17.1 executables, and what it takes to support them all

Three Cogmind executables now carry the version string "Beta 17.1" and none of
their addresses are interchangeable. The second moved only code. The third, from
Steam, moved `.data` as well — which broke the assumption the first two had made
look safe, that data addresses carry across builds. This note records what was
compared, what the conclusions rest on, and what is still owed a live check.

The build table these findings feed is
[`statmind_build.h`](../../StatMind/SDL-1.2/src/statmind_build.h), and
[`verify_retail.py`](../verify_retail.py) is the tool that re-checks all of it
against an executable without launching anything.

## The two builds

| | first 17.1 | second 17.1 | Steam |
|---|---|---|---|
| Size | 9,347,072 | 9,347,584 | 9,357,312 |
| SHA-256 | `4bcccd56…cd83` | `6c96192b…8184` | `6d24c525…1666` |
| PE timestamp | `0x6A8CFC58`, 2026-08-25 02:22:16 | `0x6A9CBFDF`, 2026-09-06 01:20:31 | `0x6A9CC02C`, 2026-09-06 01:21:48 |
| Image size | `0x940000` | `0x940000` | `0x941000` |
| Source | — | `COGMIND_Beta_17.1.zip` | Steam install |

The Steam build is the same patch (260906) as the second, compiled 77 seconds
later against `steam_api`. Both are kept unmodified under `releases/`, each with
a `manifest.json` recording where it came from. Nothing in `releases/` is ever
patched in place.

Both are PE32 i386, `ImageBase 0x00400000`, `DllCharacteristics = 0x8100` — no
`DYNAMIC_BASE` and no `.reloc` section — so each still loads at a fixed base and
every VA below is literal at runtime.

## What moved

Section RVAs are identical in both. `.text` grew by 416 bytes and `.rdata` by
160; `.data` did not change size at all. Code diverges from `0x00401178`, the
very start of the section, and the displacement is not constant:

| Anchor | first 17.1 | second 17.1 | Δ |
|---|---|---|---|
| `LuigiAi::initialize` | `0x00434C00` | `0x00434CD0` | +208 |
| `Scorekeeper::outputScoresheet` | `0x00474B90` | `0x00474A20` | −368 |
| `isDump` test | `0x00474BD0` | `0x00474A60` | −368 |
| `scorehistory.txt` guard | `0x0047E920` | `0x0047E7B0` | −368 |
| writer epilogue | `0x0047F43D` | `0x0047F2CD` | −368 |
| `cellAt` call site | `0x00484CD7` | `0x00484B67` | −368 |
| LuigiAI map-entry gate | `0x0070E358` | `0x0070E338` | −32 |
| `outputScoresheet` call site | `0x007D640B` | `0x007D66BB` | +688 |
| `cellAt` | `0x009CF7D0` | `0x009CEDA0` | −2608 |

Nine anchors, five distinct deltas, both signs. **There is no single offset to
add.** Rebasing the old addresses by any constant lands four of these nine in
the middle of unrelated functions, and the shim's whole reason for existing is
that it writes to the game's memory and calls into it directly. So the table is
per-build, keyed by PE timestamp, with every entry fingerprinted.

## What did not move — between the first two

`.data` starts at RVA `0x008A8000` with virtual size `0x000943FC` in both. The
initialised bytes are identical except for five dwords, and all five are
pointers into `.text` or `.rdata` that the linker fixed up to the new code:

| Address | first 17.1 | second 17.1 | Points into |
|---|---|---|---|
| `0x00CA91D0` | `0x00A438C0` | `0x00A43E90` | `.text` |
| `0x00CAECD8` | `0x004091B0` | `0x004091D0` | `.text` |
| `0x00CEA004` | `0x00BFFEF0` | `0x00BFFFA0` | `.rdata` |
| `0x00CEA788` | `0x00C02240` | `0x00C022D8` | `.rdata` |
| `0x00CEA78C` | `0x00C02428` | `0x00C024B8` | `.rdata` |

That is the load-bearing result for these two: an identical `.data` start, an
identical virtual size, and an identical initialised image mean the linker
placed every static global — zero-fill tail included — exactly where it had
been. Every fixed data address the harness uses survives between them.

**This is the thing not to generalise.** It made data addresses look like a
property of "Beta 17.1" rather than of a particular executable, and the Steam
build is the counterexample.

## What the Steam build moved

`steam_api` pushes `.text` 0x1C50 bytes further, past a page boundary, so the
image is a page larger and everything after `.text` slides up:

| | second 17.1 | Steam |
|---|---|---|
| Image size | `0x940000` | `0x941000` |
| `.rdata` RVA | `0x761000` | `0x762000` |
| `.data` RVA / vsize | `0x8A8000` / `0x943FC` | `0x8A9000` / `0x944D4` |

`.data` grew by 0xD8 as well as moving, so the globals inside it do not even
shift uniformly:

| | second 17.1 | Steam | Δ |
|---|---|---|---|
| `luigiAiActive` | `0x00CEFB3E` | `0x00CF0C0B` | +0x10CD |
| `LuigiAi` | `0x00CEBFFC` | `0x00CED0CC` | +0x10D0 |
| view origin | `0x00CD8FA4` | `0x00CDA074` | +0x10D0 |
| map object | `0x00CFD44C` | `0x00CFE528` | +0x10DC |
| `Scorekeeper` | `0x00D2C658` | `0x00D2D738` | +0x10E0 |

Five addresses, four different deltas. So data addresses live in the build table
per build, exactly like the code anchors, and the resolver checks each one
against the instruction that encodes it. The image-size check moved into the
table too, since `0x940000` is no longer universal.

### The reader had to learn about builds

StatMind reads these addresses from *outside* the process and had them as `const`s
in `cells.rs` and `blit.rs`, with no notion of which executable was running. That
was only ever safe because the shim refused unknown builds and the two known ones
agreed.

The shim now publishes what it resolved — build stamp, LuigiAi, map object,
player record, view origin — in `StatmindLuigiStatus`, and `common::get_addrs`
reads it. `statmind_build.h` stays the single description of a build, and the
reader has no addresses of its own. Field *offsets* stay where they were; only
the bases became dynamic.

The scoresheet descriptor survives too. The serialised `FileDescriptorProto`
protobuf leaves in `.rdata` moved with the section, from file offset `0x81A080`
to `0x81A2F8`, but it is the same 77,315 bytes, and `extract_proto.py --exe`
emits a byte-identical `.proto` from either. That tool searches for the
`0A 14 "web/scoresheet.proto"` anchor rather than seeking to an offset, so it
needed no change; the offset quoted in older notes is specific to the first
build.

## How each address is pinned

The resolver matches the PE timestamp first, then refuses the build unless every
one of these instruction fingerprints matches. A timestamp alone would be a
version check, and version checks are exactly what stops being true.

- **`LuigiAi::initialize`** — the two magic stores, `mov dword [eax], 0x64ADFA4C`
  at +10 and `mov dword [ecx+4], 0x79533ED9` at +19. Immediates, not strings, so
  they survive whatever the string table is doing.
- **The map-entry gate** — sixteen bytes reading
  `movzx eax, byte [0x00CEFB3E]; test eax, eax; jz +10; mov ecx, 0x00CEBFFC`.
  One fingerprint pinning both `luigiAiActive` and the `LuigiAi` instance, and
  proving in passing that they are the same two addresses in both builds.
- **`Scorekeeper::outputScoresheet`** — its prologue, the two `[ebp+0xC]` reads
  that locate the `isDump` parameter and show the `scorehistory.txt` append
  really is guarded by it, and the `ret 8` epilogue that pins the calling
  convention.
- **The writer's call site** — `push 1; lea eax, [ebp-0x2C]; push eax;
  mov ecx, 0x00D2C658` followed by a `call` whose relative target must resolve
  to the function just fingerprinted. The `this` pointer is taken from the
  game's own call site rather than from a literal, and that cross-check is what
  makes it trustworthy.
- **`cellAt`** — 35 bytes, byte-identical across both builds:

  ```text
  55                 push ebp
  8B EC              mov  ebp, esp
  51                 push ecx
  89 4D FC           mov  [ebp-4], ecx          ; this
  8B 45 FC           mov  eax, [ebp-4]
  8B 4D 08           mov  ecx, [ebp+8]          ; x
  0F AF 48 04        imul ecx, [eax+4]          ; * this->height
  03 4D 0C           add  ecx, [ebp+0xC]        ; + y
  8B 55 FC           mov  edx, [ebp-4]
  8B 42 08           mov  eax, [edx+8]          ; this->cells
  8D 04 88           lea  eax, [eax+ecx*4]
  8B E5 5D C2 08 00  ret  8
  ```

  This is the `{ int width; int height; Cell **cells; }` layout and the
  `x * height + y` indexing the cell reader assumes, read straight out of the
  binary. A second overload taking a `Coord*` — 2,659 call sites to `cellAt`'s
  1,736 — computes the same index from `[coord]` and `[coord+4]`, which confirms
  the field offsets independently.
- **A `cellAt` call site** — `mov ecx, 0x00CFD44C` followed by a `call` that must
  resolve to the fingerprinted `cellAt`. All 5,304 references to the map object
  in either build are `mov ecx, imm32`: it is a global singleton, always used as
  `this`, and 1,736 of those calls go to `cellAt`.

The map object is checked by the shim even though the shim never reads the cell
table. StatMind reads it from outside the process, at a fixed VA it has no way
to fingerprint from there, so the check belongs where a failure still prevents
something — before the gate byte is written at all.

**The resolver only ever compares `.text`.** Live `.data` reads as the linker
left it during `SDL_Init` and as the game has since rewritten it a moment later,
so a fingerprint over it would pass or fail depending on when it ran. Static
data is checked from the file on disk instead, by `verify_retail.py`.

## The dormant LuigiAI features are still dormant

Setting `luigiAiActive` restores what the gate guards, but it cannot restore
code that was compiled out. In the first build `updateLuigiAiMapTile` is a
five-byte stub — `push ebp; mov ebp, esp; pop ebp; ret` — so `LuigiAi.mapData`
is allocated by `loadMap()` and never filled, and every tile stays `NO_CELL`.

That is unchanged in the second build. Each executable contains **exactly one**
empty-bodied function that is ever called, and in both it has **exactly 215 call
sites**: `0x009F2CE0` in the first, `0x004F0A50` in the second. Being the unique
such function in each image identifies it without needing to match a call site
across builds, which relative operands make unreliable.

So the cell table remains the map channel on both builds, and nothing about the
flag changes that. `actionReady` and `LuigiAi.player` are dead on the second
build too, confirmed live: `actionReady=0` and `LuigiAi.player` NULL while a map
was loaded and the player was moving. What the flag *does* restore is live there
as expected — `map=75x75`, `depth=-10`, `mapData=0xD5E002C`.

One consequence worth remembering: because `actionReady` never changes, a move
that really happened still reports `no turn advanced (actionReady still 0)`.
That message is a dead field talking, not a failed input. Confirm a move by
re-reading the player record, never by that string.

## Finding these on the next build

[`find_build.py`](../find_build.py) does the whole search: the nine anchors from
their fingerprints, the four data addresses from the instructions that encode
them, and — given `--reference` — the two that nothing encodes. It prints a
table row ready to paste.

It is validated by reproducing rows already shipped:

```sh
python3 find_build.py --self-test "path/to/COGMIND.exe"
```

Both Beta 17.1 rows reproduce exactly, and running it against the Steam build
with the second as reference produces the row now in the table.

## What static analysis cannot settle

Two of StatMind's four fixed data addresses have no code to point at.

`VIEW_ORIGIN` at `0x00CD8FA4` at least sits in initialised data, and its
initialiser is `{27, 8}` in both builds — the same pair the address was
originally found by scanning for. `verify_retail.py` checks it outright.

`PLAYER_REC` at `0x00D2D338` has **zero** references in either `.text`. It is
reached through a pointer, which is why it was found by differential scan in the
first place. Its neighbourhood is unchanged — the nearest referenced globals on
either side are `0x00D2D2A0` (63 references) and `0x00D2D348` (2), identically in
both builds — and the `.data` argument above covers it. But it is inferred from
layout, not pinned, so it needed a live check.

On the Steam build it is **not yet known**, and its table entry is `0`. The
reader refuses player queries rather than falling back to another build's value,
because a wrong address here does not error — it returns plausible-looking
coordinates. `find_build.py` derives a candidate of **`0x00D2E410`** by matching
the neighbouring global (itself identified by the bodies of the methods called on
it, which are byte-identical across builds) and taking the same +0x98 offset.
That candidate has not been read on a running game and must not go in the table
until it has.

**Confirmed on the second build, 2026-09-17**, against a running game on the
worker (seed `8STBK809`, `MAP_MAT` at depth −10, the game reporting its own
build as `260906a`):

- The record read `pos=(65,23) handle=0x007C0000` and resolved to entity
  `Cogmind`.
- Probing the player's own cell returned `decoded.x/y = 65,23` — equal to the
  requested coordinates, so the `Cell` offsets and the column-major index are
  right for this build — and `entity = 0x007C0000`, **the same handle**. That
  agreement between the cell table and the record is the real confirmation;
  `plausible` alone only checks the coordinates are in range.
- Two moves tracked exactly: `(65,23)` → west → `(64,23)` → north → `(64,22)`.
- `VIEW_ORIGIN` tracked with them, `(59,22)` → `(58,22)` → `(58,21)`, so it is
  live on this build and not merely sitting at its initialiser.

The writer was exercised the same way. `outputScoresheet` at the second build's
`0x00474A20` was called three times from the `SDL_Flip` hook; the game survived
all three, the dumps landed in `dumps/`, and `scorehistory.txt`'s md5 and the
`scores/` count were both unchanged — so the `isDump` guard still suppresses the
append, which is what stops `episode.py` reading a dump as a finished run.

## Adding the next build

1. Keep the executable: `releases/<version>-<sha256 prefix>/`, with a
   `manifest.json` recording where it came from, its hash and its size.
2. Run `find_build.py NEW.exe --reference KNOWN.exe`. Never rebase a known
   build's addresses by a constant; no two builds so far have differed by one.
3. Paste the row into `statmind_builds[]` in `statmind_build.h`, leaving the
   player record `0`.
5. Run the verifier over every supported executable:

   ```sh
   python3 verify_retail.py "path/to/COGMIND.exe" ...
   ```

   It compiles the real resolver from the shipped header rather than a copy of
   it, accepts each build, and then flips a bit at each of 24 trust anchors in
   turn and requires the resolver to reject all 24. A fingerprint that cannot
   fail closed is not a fingerprint.
6. Rebuild the shim (`./build-sdl.sh`) and the reader (`./build-statmind.sh`),
   and confirm the shim logs the build it resolved at startup.
7. Run the game, confirm the player-record candidate against the cell table, and
   only then put it in the table.

An unrecognised executable is not a soft failure. `Statmind_FindBuild` returns
`NULL`, the gate byte is never written, `outputScoresheet` is never called, and
both subsystems log why and stay off.
