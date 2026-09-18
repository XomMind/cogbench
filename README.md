# Cogbench

Watch and control a model playing Cogmind, or compare it with scripted policies.
The game runs on the Kubernetes worker; your laptop opens the controls and video.

Cogmind and its assets are not included; this is unofficial, independently
developed tooling and is not affiliated with or endorsed by Cogmind's
developer. You need your own legally obtained copy of the game to run it.

## Start here

From the `cogbench` folder or `harness` folder on your Mac:

```sh
just watch       # open the live game; click Sound on for audio
just control     # open Start / Stop, model selection, and settings
```

No terminal needed for watching: [open the game](http://slacker.local:31724/)
or [open the controls](http://slacker.local:31720/control.html).
These addresses require access to the worker's network.

| I want to… | Go here |
|---|---|
| Put the game and stats in OBS | [OBS setup](OBS.md) |
| Connect my laptop or fix an unreachable worker | [Setup and troubleshooting](SETUP.md) |
| Maintain or troubleshoot Kubernetes | [Worker guide](k8s/README.md) |
| Play by sending game commands | [Game command guide](AGENT.md) |
| Understand the bridge or build StatMind | [StatMind README](../StatMind/README.md) |
| Check or add a supported game build | [Build notes](notes/retail-17.1.md) |
| Read the reverse-engineering history | [Research notebook](notes/b17.1-luigiai.md) |

Run `just` to see all available commands, or `just urls` to print the addresses.

## Everyday controls

Use the control page, or these terminal shortcuts:

| Command | What happens |
|---|---|
| `just status` | Show agent status and the last result |
| `just start` | Start the agent with saved settings on the current episode |
| `just stop` | Stop the agent and cancel pending automatic revival; keep the game open |
| `just restart` | Restart the agent on the same episode |
| `just models` | List available model IDs |
| `just model MODEL_ID` | Switch the live model |
| `just logs` | Read recent agent messages |
| `just loop on` / `just loop off` | Allow or prevent a new episode after the game ends |
| `just revive on` / `just revive off` | Allow or prevent recovery after an agent crash |

**Restart restarts the agent, not the episode.** A decision budget running out
also leaves the current episode intact.

| Result | Meaning |
|---|---|
| `ended` | The game ended; Loop episodes may start a new one |
| `budget_exhausted` | The agent reached its decision limit |
| `blocked` | The agent could not recover the game screen; auto-revive may retry |

A stopped or crashed agent may leave no result. That does not mean the game ended.

## For developers

The main files are `agent.py` (decisions), `runner.py` (agent lifecycle),
`stream.py` (controls and overlays), `webstream.py` (browser video/audio), and
`cogbench.py` (game commands).

```sh
just test        # local regression tests
just check       # tests plus syntax and whitespace checks
just changes     # show pending changes in harness and StatMind
```

Tests do not start the game, call a model, or change the worker. With FFmpeg and
libx264 installed, they also check synthetic video encoding and decoding.
Actual gameplay, OBS, and browser/device checks are separate.

For a direct agent run, execute this where the game and StatMind are available
(the worker, when using Kubernetes):

```sh
export COGBENCH_URL=http://127.0.0.1:8128/v1  # replace with your model endpoint
python3 agent.py --statmind /usr/local/bin/statmind \
  --policy model --model MODEL_ID --decisions 200 --stream --out result.json
```

Set `COGBENCH_API_KEY` if required. Policies `heuristic` and `scripts` need no
model endpoint. `--cheat` exposes ground-truth terrain; compare those runs
separately from fair-view runs. The supervisor saves `run-result.json` in
`COGBENCH_STREAM_DIR` and includes it in status after the agent exits.
