# Early macOS setup notes (historical)

This preserves the original setup log. “Verified”, “untested”, and “not yet
implemented” describe the work at the time, not the current checkout. Some
commands contain machine-specific paths. For everyday use, start with
[the current setup guide](../SETUP.md). For later findings, see
[the research notebook](b17.1-luigiai.md).

---

# Cogbench harness — end-to-end setup

Status of each step is marked. **Verified** means it was actually run.
**Untested** means it is written and type-checks but has never touched a live game.

Target: Cogmind Beta 17.1 (`COGMIND.exe`, 9,347,072 bytes) under the Sikarugir
wrapper at `/Applications/Cogmind.app`.

---

## 0. What this does

`-luigiAi` no longer exists as a switch in Beta 17, but most of the LuigiAI
subsystem is still compiled in and gated on a single byte. The patched SDL shim
sets that byte from inside the process at `SDL_Init`; StatMind reads the struct and
injects keys back through the shim's mailbox; `cogbench.py` turns that into a shell.

> **The flag alone does not give you map data.** `updateLuigiAiMapTile` is an
> empty function in 17.1, so `mapData` is allocated and never filled — every tile
> stays `NO_CELL`. What the flag *does* restore: map dimensions, depth, map type,
> `mapCursorIndex`, `player`, and `machineHacking`.
>
> Map content has to come from the game's own cell table instead, at
> `0x00CFD454` (`Cell**`, `width*height`, index `x*height + y`), with `width` at
> `0x00CFD44C` and `height` at `0x00CFD450`. That read needs no patch, works on
> 17.1 today, and is also the FOV-independent privileged channel. It is **not yet
> implemented** — the `Cell` layout is the open reverse-engineering item.

Full reverse-engineering detail: [`notes/b17.1-luigiai.md`](b17.1-luigiai.md).

---

## 1. Build the patched SDL.dll  *(verified)*

> `touch StatMind/SDL-1.2/src/SDL.c` before building after editing any
> `statmind_*.h`. The SDL makefile does not track those headers as dependencies,
> so the build reports "Nothing to be done" and ships the previous DLL. `SDL.c` is
> the single translation unit that includes them.


The patch lives in `StatMind/SDL-1.2/src/statmind_ipc.h` and is pulled in by
`SDL.c`, which already calls `Statmind_StartIPC()` from `SDL_Init`.

The game directory already contains an `SDL.dll` you built on 2026-07-31, so reuse
whatever produced it — the source change needs no build-system change. For a
mingw cross build the shape is:

```bash
cd StatMind/SDL-1.2
./autogen.sh
./configure --host=i686-w64-mingw32 --disable-static --enable-shared
make -j
```

Then drop `SDL.dll` into `/Applications/Cogmind.app/Contents/SharedSupport/prefix/drive_c/COGMIND (Beta 17.1)/`,
keeping a copy of the original first.

**What the patch does** (`Statmind_EnableLuigiAi`):

- resolves the image base via `GetModuleHandleA(NULL)`, checks `MZ` + `PE`
- fingerprints `LuigiAi::initialize()` by its two magic-store instructions at
  RVA `0x34C0A` / `0x34C13` — build immediates, so they survive whatever the
  string table is doing, and they pin the build far harder than a version check
- `VirtualQuery`/`VirtualProtect` on the target byte, then writes
  `luigiAiActive` (RVA `0x8EFB3E`, VA `0x00CEFB3E`) = 1
- exports `g_statmind_luigi` (magic `0xBADBEEF2` / `0x79533EDA`) carrying status
  and the resolved base / flag / struct addresses
- re-asserts the flag once a second from the IPC thread

Any build that fails the fingerprint gets `LUIGI_ST_SIG_MISMATCH` and **no write**.

Environment:

| Variable | Effect |
|---|---|
| `STATMIND_LUIGI=0` | skip the patch entirely |
| `STATMIND_LUIGI_TEST=1` | also set `0x00CEFB36`, the probable `luigiAiTest` |

### Mailbox protocol v2

The shim's command channel was `{u32 magic; u32 check; char command; u8 data; char pad[2]}` —
one keysym, no modifiers, no unicode, no mouse. That cannot express Cogmind's input
grammar: 96 of the 493 bindings carry at least one modifier, and text fields read
`SDL_keysym.unicode`, which v1 always left at 0.

v2 keeps `magic`/`check` at `+0x00`/`+0x04` so existing memory scans still find it.
`sizeof == 108`, verified against a compiled `offsetof` dump:

| Field | Offset | |
|---|---|---|
| `magic` / `check` | `0x00` / `0x04` | unchanged, so the scanner still works |
| `version` | `0x08` | `2`; StatMind refuses to talk to a mismatched shim |
| `seq` | `0x0C` | writer increments **last** to submit |
| `ack` | `0x10` | shim copies `seq` here when done |
| `status` | `0x14` | `1` ok, negative on rejection |
| `command` | `0x18` | `K` key, `T` text, `M` mouse warp, `B` click |
| `button` | `0x19` | |
| `keysym` / `modifiers` / `unicode` / `repeat` | `0x1A`–`0x21` | |
| `text_len` | `0x22` | |
| `mouse_x` / `mouse_y` | `0x24` / `0x28` | |
| `text[64]` | `0x2C` | |

Three things this fixes beyond expressiveness:

- **`seq`/`ack` separates delivery from turn advance.** The old code inferred delivery
  from `LuigiAi.actionReady` changing, so any key that only opened a UI panel looked
  like a 5-second timeout. Now `submit()` waits for the ack (2s), then *optionally*
  watches `actionReady` (750ms) and reports which happened.
- **Modifiers are mirrored into `SDL_SetModState()`** around the synthetic press, then
  restored. Cogmind does not read modifiers only from `SDL_keysym.mod`; some paths call
  `SDL_GetModState()`.
- **Mouse uses `SDL_WarpMouse`** rather than synthetic motion events, because
  cursor-driven targeting reads the real pointer position.

---

## 2. Build StatMind  *(verified)*

```bash
./build-statmind.sh          # cargo build --release, then re-sign
```

Use the script, not bare `cargo build`. **Every** cargo build replaces the binary
and drops the code signature with it, and without the signature's debugger
entitlement `task_for_pid` fails at attach time as

```
Failed to get task port for PID <n>: 5
```

which reads like a permissions problem with the game rather than a stale build.
The script does both steps and then asserts the entitlement survived.

The repo did **not** compile as cloned — the `feat: add mcp server` commit was
never built. Fixed here:

- `#[repr(i32)]` enum fields held raw game values, and the game legitimately
  writes `NO_CELL = -1`. An out-of-range discriminant in such an enum is
  undefined behaviour in Rust, and it is why the generated enums could not derive
  `Serialize`. Raw ids in `types.rs` are now `i32`, resolved to names on the way out.
- map walk indexed `y*map_width + x` and then swapped the emitted coordinates.
  `luigiai.h` specifies `access = x*mapHeight+y` — column-major. The two errors
  cancelled only on square maps. Now walks columns properly.
- `player_x` / `player_y` are emitted, found by matching `tile.entity` against
  `LuigiAi.player`. Previously the agent had no way to locate itself.
- `LuigiEntity.inventory` is now walked, so attached parts and cargo are visible.
- **root cause of the broken build:** `src/generated.rs` is committed *and*
  regenerated by `build.rs` on every build. Someone had hand-edited the committed
  copy to add `Serialize`/`Deserialize` derives and a `NO_CELL = -1` variant;
  `build.rs` silently overwrites both on the next `cargo build`. Consider emitting
  to `OUT_DIR` and `include!`-ing it, or gitignoring the file — otherwise any fix
  applied there disappears. The fix below deliberately does not rely on hand-edits
  surviving.
- added `const _: () = assert!(size_of::<LuigiTile>() == 28);` — `loadMap()`
  allocates `count * 0x1C + 4`, so 28 is the game's own number. **This assertion
  passes.**

---

## 3. macOS privileges  *(pre-existing, unchanged)*

StatMind reads the game out-of-process via `task_for_pid`, which needs
`system.privilege.taskport` and an ad-hoc-signed binary. `build-statmind.sh` (or
`runner.zsh`) does the codesign; both take the signing identity from
`STATMIND_CODESIGN_ID` — set it for your machine. Expect an auth prompt.

Re-sign after *every* rebuild; see section 2.

> This whole step disappears once the reader moves in-process alongside the Lua
> VM, which is the intended direction. It is also the reason the current harness
> cannot fan out on Linux.

---

## 3b. Generate the action space  *(verified)*

```bash
./gen_actions.py --user-dir ~/Documents/Cogmind/user -o actions.json
```

Cogmind writes its own binding tables when `exposeKeybinds=1` is set in
`advanced.cfg`. `keyboard.cfg` is a name → SDLK table *indexed by keycode*;
`commands.cfg` is every command grouped by UI domain with its exact chord.
Together they are the complete input grammar, per build.

Current numbers for this profile: **329 commands, 493 bindings, 33 domains,
0 unresolved key names, 0 commands without a usable binding, 0 intra-domain
keystroke collisions.** The keysym table validates exactly against SDL 1.2
(`a`=97, `KP8`=264, `UP`=273, `F1`=282, `LSHIFT`=304).

Re-run this after any version bump — a Cogmind update then produces a diff
instead of a debugging session.

Worth knowing about, found this way: `CMD_BS_DEFAULT_KEYBOARD_AUTOPATH`
(built-in travel, so "go there" may not need harness-side pathfinding),
`CMD_BS_DEFAULT_GET` / `GET_ATTACH`, `CMD_BS_DEFAULT_RUN_*`, `MOVE_UP`,
`LABEL_EXITS`, and `CMD_BS_DEFAULT_OUTPUT_MAP`, which dumps the map to a file
and is another ground-truth oracle needing no patch at all.

---

## 3c. Stage a clean test install  *(script written, unrun)*

There is a blank retail Beta 17.1 in `~/Downloads`. Use it rather than the live
install, so nothing under test touches your own profile:

```bash
SDL_DLL=/path/to/patched/SDL.dll ./stage-test-install.sh
```

It rsyncs the retail copy to `drive_c/COGMIND-COGBENCH`, keeps the stock DLL as
`SDL.dll.stock`, and creates a fresh profile at `~/Documents/Cogmind-cogbench`
with `exposeKeybinds=1`. Copies only — it never edits the source install or the
app bundle. It then prints the two `Info.plist` fields to change by hand.

---

## 4. Run  *(untested end-to-end)*

```bash
open -a Cogmind                                    # or launch via the wrapper
export STATMIND_BIN=/path/to/StatMind/target/release/statmind
./cogbench.py daemon                               # keep this running
```

In another shell:

```bash
./cogbench.py look
./cogbench.py move ne
./cogbench.py parts
./cogbench.py repl                                 # or drive it interactively
```

The daemon must stay up: StatMind caches the mach task port and the mailbox
address, and reacquiring them is slow and re-prompts.

---

## 5. Verification order

Work down this list. Each step is meaningless until the one above it passes.

1. **Did the flag get set?** Scan the running process for `0xBADBEEF2` followed by
   `0x79533EDA` and read `status`. `1` = enabled, `2` = already set,
   `-4` = fingerprint mismatch. Or just watch stdout for `[Statmind_Luigi]`.
2. **Did the struct populate?** `cogbench.py state` should show a non-zero
   `map_width`/`map_height` once you are in a map. If the magics are found but the
   map stays 0x0, the flag was set too late — it must precede the map-load path.
   Expect the tile list to be **empty** regardless: the mirror is stubbed. Dimensions
   non-zero plus zero tiles is the correct, expected result on 17.1.
3. **Is `actionReady` live?** This is now the load-bearing unknown — no
   absolute-address writer exists for `luigiAi+0x08`, and the whole action
   handshake reads it. Watch whether it increments as you take turns. If it never
   moves, `submit()` will always report "no turn advanced" and turn detection needs
   another source.

3b. **Are the coordinates right?** Only testable once cell reads land. `look`, then
   `move e`, then `look`: exactly one coordinate should change, and it should be `x`.
   The formula is already confirmed twice from the binary (`cellAt` and the cursor
   index both compute `height*x + y`).
4. **Is the inventory stride right?** `parts` should list plausible part names with
   sane integrity. The default of 12 is now backed by the binary: b14's mirror
   allocates `operator new(0xC)` for the field at `LuigiTile+0x18` (item), so
   `sizeof(LuigiItem) == 12` and the public header's `equipped` bool is real.
   `STATMIND_ITEM_STRIDE=8` remains available if 17.1 turns out to differ.
5. **Are the Cell offsets right?** `probe <x> <y>` on the cell you are standing on.
   `decoded.x`/`decoded.y` must equal what you asked for, `entity` must be non-zero,
   and `cell_name` should be a plausible floor type. If x/y are transposed or wrong,
   either the Cell offsets or the column-major index is wrong for this build.
   A `cell_id` with a `null` `cell_name` means the ID tables need regenerating
   (they are Beta 16), not that the read failed.

5b. **Find the FOV byte.** `get_map` is not FOV-filtered, so `look` currently shows
   ground truth — every robot, through walls. That is correct for a critic channel
   and wrong for an agent observation. Stand in a lit corridor, `probe` a lit cell
   and an unlit one at similar distance, and diff the `0x50`-byte dumps. The flag
   is almost certainly a byte near `doorOpen` at `+0x3A`. Watch for two separate
   flags (visible now vs. seen before), and for a third if sensor detection is
   modelled apart from line of sight.

5c. **Which byte is which?** Run once with `STATMIND_LUIGI_TEST=1` and see whether
   `luigiAi/test.txt` appears. That confirms `0x00CEFB36` is `luigiAiTest`.
6. **Do the ID dumps work?** Look for `~/Documents/Cogmind/luigiAi/` after enabling.
   If `cellID.txt`, `propID.txt`, `entityID.txt` and `itemID.txt` appear, the path
   strings are encrypted at rest rather than removed, and you have authoritative
   Beta 17.1 tables — replace `StatMind/src/*.txt` and rebuild so `build.rs`
   regenerates `generated.rs`. The tables in the repo are Beta 16.

---

## 7. Episode lifecycle  *(verified)*

`episode.py` handles seeds, fresh runs, and run-end detection.

```bash
./episode.py init --seed COGBENCH1 --force   # create the isolated profile
./episode.py launch --seed COGBENCH1          # fresh seeded run
./episode.py fingerprint                      # hash the map (determinism check)
./episode.py status                           # profile, seed, difficulty, last run
./episode.py watch                            # block until the run ends
./episode.py history                          # parse scorehistory.txt
```

### Isolated profile, not the player's

Episodes run against `~/Documents/Cogmind-bench`. That gives a `scorehistory.txt`
starting at zero rows (so run detection is trivial), no interference with real
saves, and -- because the profile has no save file -- **launching necessarily
starts a new game**. `open -a Cogmind` against a profile *with* a save silently
resumes it; the binary even contains a "Resuming world seed: " string.

### Seeds are deterministic  *(verified)*

`worldSeed` in `options.cfg`, set before launch. In-game help: "Setting this only
affects future games."

| run | seed | map fingerprint |
|---|---|---|
| A | `COGBENCH1` | `6e346a4203eb37c6` |
| B | `COGBENCH1` | `6e346a4203eb37c6` |
| C | `COGBENCH2` | `1121a3191c1a26b0` |

Same seed reproduces the world; a different seed does not. `set_seed` refuses to
write while the game is running, because Cogmind rewrites `options.cfg` on exit
and the edit would vanish without an error.

### Launching directly  *(three traps)*

`-customFilePath` cannot be passed through `open -a`, so `launch-direct.sh` calls
the wrapper's wine itself. Three things fail confusingly:

1. **cwd must be the game directory**, or the game dies with
   `FATAL ERROR: init | Unable to open object data` -- it resolves its data
   archive relative to cwd.
2. **`DYLD_FALLBACK_LIBRARY_PATH` must include `Contents/Frameworks`**, or
   wineserver cannot load `libinotify.0.dylib`.
3. **Do not wrap the launch in `nohup`.** It lives in SIP-protected `/usr/bin`,
   so macOS strips every `DYLD_*` variable, silently reintroducing (2).

### Run-end detection

Three signals, cheapest first: `stats.integrity <= 0` (in-memory, immediate);
a new row in `scorehistory.txt` (authoritative, carries score, deepest location,
mode and seed); a new file under `scores/`. The binary notes
"(no scoresheet for suicides below depth 9)", so a full scoresheet is not
guaranteed for every ending -- `scorehistory.txt` is what `result()` reads.

**Verified against 152 real runs** from the player's own profile, spanning six
Cogmind versions and including 15 wins, plus a stale-baseline test confirming the
watcher fires and parses correctly. Three parser bugs were found that way:

* the header is **multiple** lines (title, column header, a continuation header,
  and a dashes rule) -- prefix matching let two junk rows through as "runs", so
  rows are now accepted only if they *parse* with a version and integer score;
* **locations can be multi-word** (`Frmr TC4`, `Tau Ceti` -- 6 of 152 rows),
  which shifts every later column;
* the positional pass was **overwriting** the right-anchored `mode`/`seed`.

Verified against a live death as well: on death the watcher fired via
`scorehistory`, the row parsed, and both score files were written -- with the seed
that had been set before launch.

---

## 8. The stat-dump observation channel  *(verified live)*

Cogmind serialises the run in progress on demand. `Alt-Shift-S` does it in-game;
the shim calls the same writer directly, which also works with a menu open (the
keybind is confined to one UI domain).

```bash
./cogbench.py dump              # one round trip -> a compact observation
./cogbench.py dump path         # the .json just written
./cogbench.py raw dump_status    # resolution state, addresses, last result
./statdump.py obs               # re-read the newest dump
./statdump.py map --player 58,51 # its known-map window, labelled with game coords
./statdump.py prune --keep 20    # nothing rotates dumps; call between episodes
```

Needs `jsonStatDump=1` in the profile's `advanced.cfg` (`episode.py init` sets
it). Output lands in `<profile>/dumps/`, **not** `scores/`.

Why it matters: it is the only source for resource **maxima** (derived from
attached parts, so stored nowhere), for the loadout by slot and name including
inventory, for the recent message log, and for a **field-of-view-correct map** --
`map.lines` masks unexplored cells to `?`, which the cell-table reader behind
`look` does not do.

`map.lines` is a 50x50 window centred on the player, so
`game_x = col + player_x - 25` and likewise for `y`. Verified against the cell
table, and it moves with the player. It is therefore a *local* view of the known
map, not all of it.

Three properties worth knowing before wiring it into scoring:

* A dump **does not append to `scorehistory.txt`** -- guarded by the same
  `isDump` flag, checked in the disassembly and confirmed by md5 across three
  consecutive dumps. Without that, every dump would look like a completed run to
  `episode.py`.
* It advances no turn and costs 17-109 ms.
* Keys are **camelCase** (`totalScore`, `runResult`), not the schema's
  snake_case. Proto field names silently return nulls.

It also supplies the **turn counter** -- `stats.exploration.turnsPassed`, plus
per-action-type counts in `stats.actions.total`, globally and per map. No
monotonic turn counter was ever found in memory, so this is the only reliable
source.

The other `OUTPUT_*` command, `Alt-Shift-M` (Output Map), writes a 3600x3600
**PNG** to `screenshots-maps/` as `<SEED>_<depth>_<Map>_mapturn_<turn>.png`. It
is an image, not a text export, so it does not widen `map.lines` -- but the
filename carries seed, depth, map and turn, and the image itself is a ready-made
vision channel.

The schema itself is recovered from the binary, where protobuf's generated code
leaves the serialised `FileDescriptorProto` verbatim in `.rdata`:

```bash
./extract_proto.py --summary --verify
./extract_proto.py -o schema/scoresheet-b17.1.proto
```

30 messages, 12 enums, `--verify` cross-checks the extracted bytes with
`protoc --decode`. This is the schema for the build you are actually running,
which the public prerelease repo is not. See notes/b17.1-luigiai.md round 14 for
the addresses and the calling convention.

---

## 6. Known gaps

**Closed since the first pass:** the action space now covers all 329 commands,
modifiers and unicode work, mouse warp and click exist, and delivery is
distinguished from turn advance. The **episode lifecycle** exists and is verified
against a live death (§7). A **field-of-view-correct observation** exists too, via
the stat dump's known-map window (§8).

**Still open:**

- **Targeting is expressible but not modelled.** `fire` sends `f`; the harness has
  no notion of the `BS_TARGETING` domain's cursor, so it cannot yet aim at a
  specific tile. `mouse_move` plus `CMD_BS_TARGETING_*` are the raw material;
  `LuigiAi.mapCursorIndex` is read-only, so cursor position is observable but
  only steerable through keys and the pointer.
- **No UI-domain tracking.** `commands.cfg` tells you which domain each command
  is legal in, but nothing tracks which domain the game is *currently* in. Only
  `machineHacking != NULL` is directly observable. Until that is modelled, an
  agent can send a command that is illegal in the current mode and get a silent
  no-op. Inferring the domain from what changed after each keystroke is the
  cheap version; a second in-process read is the real fix.
- **Tile-to-pixel mapping is unknown.** Mouse commands take pixels, and nothing
  yet converts a map coordinate into one. Needed before cursor targeting works.
- **`get_map` is not FOV-filtered** and so must not be the agent's observation.
  Use the stat dump's `map.lines` instead (§8) -- Cogmind's own known map, with
  unexplored cells masked. Two caveats: it is a 50x50 window centred on the
  player rather than the whole map, and it is a report taken at a moment rather
  than a live read, so it says what was known when the dump was written. Neither
  the per-frame *currently visible* set (blit capture, §5b) nor a whole-map known
  layer is solved.
- **No turn counter in memory.** Closed in practice by the stat dump's
  `turnsPassed` (§8), but there is still no cheap in-memory read; a candidate at
  `0x00D3C258` is not monotonic.
- **`get_map` is slow.** One read per occupied cell, because cells are individually
  heap-allocated. A viewport is fine; a full map is tens of thousands of mach round
  trips. Moving the reader in-process is the fix.
