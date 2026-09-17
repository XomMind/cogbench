# Game command guide

You are playing **Cogmind**, a turn-based sci-fi roguelike, through a shell.
Your goal is to travel from the Scrapyard toward the surface and escape.
Death is permanent — there is no retry inside a run.

## Connect first

For the Kubernetes worker, run commands from your laptop as `just game COMMAND`:

```sh
just stop          # stop the agent before taking manual control
just game look
just game parts
just game move ne
```

For a local game, start the daemon as described in [Setup](SETUP.md), then run
commands from `harness`. The examples below use that local form: replace
`./cogbench.py` with `just game` when using the worker.

[Back to the main guide](README.md) · [Watch or set up OBS](OBS.md)

## How to act

Run one command per decision:

```bash
./cogbench.py look
./cogbench.py move ne
./cogbench.py parts
```

| Command | Effect |
|---|---|
| `look [w] [h]` | render the map around you (default 80x30) |
| `who` | entities currently in your field of view, with coordinates |
| `parts` | your attached parts and cargo |
| `state` | the full game state as JSON — large, use sparingly |
| `move <n\|ne\|e\|se\|s\|sw\|w\|nw>` | move one tile |
| `fire` / `attach` | the two bound action keys |
| `get` | pick up the item you are standing on |
| `getattach` | pick up and attach in one action |
| `wait` | pass a turn |
| `run <dir>` | keep moving that way until something happens |
| `exits` / `enemies` / `partslabel` | label exits / hostiles / parts on the map |
| `status` / `intel` | your status readout / known intel |
| `up` | take the exit you are standing on |
| `cmd <CMD_NAME>` | send **any** of the game's 329 commands by name |
| `actions [filter]` | list commands, filtered by name or UI domain |
| `text <string>` | type text, for hacking codes |
| `dump` | ask Cogmind to serialise the run so far, and read it back |

Start every turn with `look`. Commands that act in the world consume a game turn;
`look`, `who`, `parts`, `state` and `actions` are free.

Every action reports whether it was **delivered** and whether a **turn advanced**.
"delivered ... no turn advanced" is normal for anything that only opens a panel or
moves a cursor — it is not a failure.

## Reading the map

```
@ you        # wall       . floor     + door    / open door
> stairs     ? robot      & prop      % phasewall    (space) no cell
```

> **Warning, current build:** the map is read from the game's own cell table, which
> is **not** field-of-view filtered. So `look` shows you robots through walls —
> ground truth, not what a player could see. Do not treat this as a fair view, and
> expect it to change once visibility filtering lands.
>
> `dump` is the fair alternative: its map is what *you* have explored, with
> everything else masked to `?`.

## `dump` — the game's own report on your run

`dump` makes Cogmind serialise the run in progress, the same report `Alt-Shift-S`
produces, and reads it back as one object. It costs no turn and it is the only
place several things are visible at all:

* **Resource maxima** — `energy: 100/250`. Nothing else in this shell can tell you
  a maximum, because maxima come from your attached parts.
* **Your loadout by name**, per section, inventory included. `parts` shows less.
* **Your explored map**, `?` for anything you have not seen. Unlike `look`, this is
  a fair view — but it is a 50×50 window centred on you, not the whole map, and
  `exits_relative_to_player` gives exits as offsets from your position.
* **The recent message log**, verbatim.
* `result_so_far` — the game's own one-line verdict on how the run is going.

Use it when you arrive on a new floor, after a fight, before deciding what to
attach, and any time you are unsure what you are carrying. It is cheap (well under
a tenth of a second) but it is a *report*, not a live feed: call it again rather
than reasoning from an old one.

Coordinates are `(x, y)` with the origin top-left. `x` grows east, `y` grows south.
The header includes your position, map size, and `actionReady`. Do not use
`actionReady` as elapsed turns; the raw stat dump supplies
`stats.exploration.turnsPassed`.

## What matters in Cogmind

- **You are your parts.** Cogmind has no HP bar in the usual sense — you have a core
  (`integrity`) plus attached parts that supply propulsion, power, utilities and
  weapons. Parts get shot off. Replacing them from wreckage on the floor is the
  core loop, not an optimisation.
- **Check `parts` often.** Losing propulsion strands you; losing power shuts down
  everything else.
- **Watch `heat`, `energy` and `matter`.** Firing costs energy and generates heat;
  overheating damages you. Matter is the currency for repairs and fabrication.
- **Choose fights carefully.** Reaching the next floor is often more useful than fighting. Fleeing to the
  exits beats winning a fight you did not need. Alert levels rise as you make noise,
  and the garrisons that respond get worse the longer you linger.
- **`>` marks an exit.** Move onto it and use `up` to take it.

## Known limits of this shell

Be aware of these — they are harness gaps, not game rules:

- **`fire` cannot aim.** It sends the fire key, but nothing places the targeting
  cursor, so it will usually not do what you want.
- **Commands are only legal in some UI modes.** `actions` shows each command's
  domain (`BS_DEFAULT` is normal play, `BS_TARGETING` is aiming, `INVENTORY`,
  `PARTS`, `HACK`, and so on). The harness does **not** track which mode the game
  is currently in, so a command sent in the wrong mode may silently do nothing.
  If an action reports no turn advanced and `look` shows no change, assume you are
  in a different mode and send `cmd CMD_COMMANDS_CLOSE` or `key ESCAPE` to back out.
- Part swapping by slot, and dialog answering, are not modelled — the keys exist
  but nothing reads the resulting menus.

If a command fails, do not retry it in a loop. Call `look`, and pick a different
action.

## Reporting

When you stop — death, escape, or budget exhausted — say which depth and map you
reached, how many turns elapsed (from the stat dump), and what killed you or blocked you.
