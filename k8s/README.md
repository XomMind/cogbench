# Kubernetes worker guide

The worker runs Cogmind, the agent, and the streams. Your Mac is the remote
control. Run the `just` commands below from `cogbench` or `harness`.

## Daily use

```sh
just doctor         # check access and services
just watch          # open game video and sound
just control        # open agent controls
just status         # read agent status
just logs           # read recent agent decisions/errors
```

For OBS, follow [the source setup guide](../OBS.md). If the worker hostname
does not connect, use [the localhost tunnel](../SETUP.md#connection-problems).

| Action | What it affects |
|---|---|
| `just stop` | Stops the agent, cancels pending revival, leaves the game open |
| `just start` | Starts the agent on the current episode |
| `just restart` | Restarts only the agent, on the same episode |
| `just loop on` | Allows a new episode after a detected game ending |
| `just revive on` | Allows automatic agent recovery after a crash |
| `just deploy` | Replaces the worker pod: game, agent, and streams are interrupted |

A decision budget ending does not authorize a new episode. Loop mode can restart
the game container after a confirmed ending. The profile persists on disk;
a worker restart is not a promise that an interrupted game saved successfully.

## Find the right logs

```sh
just pods                 # deployment, pod, service, node, and restart counts
just logs 80              # agent log
just worker-logs          # game / Wine / StatMind startup
just worker-logs overlay  # controls and overlay server
just worker-logs web      # browser stream
just worker-logs lite     # OBS stream
just worker-logs video    # silent fallback pictures
just stream-status        # browser encoder health and viewer count
```

`worker-logs` follows output; Ctrl-C only stops following it.

| Symptom | First checks |
|---|---|
| Pod is Pending | Pod events, `cogbench-data` PVC, and available node capacity |
| ImagePullBackOff | Image tag, registry access, and node reachability |
| CreateContainerConfigError | The `cogbench-endpoint` Secret exists with keys `url` and `api_key` |
| Game container keeps restarting | Game logs; `/data/game/COGMIND.exe` and patched `SDL.dll` must exist |
| StatMind permission error | Game-container `SYS_PTRACE` allowance and StatMind executable permissions |
| Agent stopped but video works | `just status`, `just logs`, decision budget, and selected model |
| Model requests fail | Endpoint availability, Secret references, and egress policy; see below |
| Browser works but OBS does not | Correct `:31723/stream.ts` URL, `lite` logs, and no second OBS/VLC client |
| OBS works but browser does not | `just stream-status` and `web` logs |
| Overlay picture freezes | `video` logs; this is separate from the browser and OBS encoders |

For pod events, run this with the context/namespace you use for Cogbench:

```sh
kubectl --context admin@dev-cluster -n cogbench describe pods -l app=cogbench
```

A Ready pod is only a starting check. The manifest probes the overlay page;
it does not prove that the agent is playing, OBS can decode video, or sound works.

## What runs where

One deployment (`cogbench`), one pod, five containers. The deployment uses
`Recreate`, so updating it stops the old pod before starting the new one.

| Container | Job |
|---|---|
| `cogbench` | Game, virtual display, audio, StatMind, and agent supervisor |
| `video` | 15 fps JPEG capture for the overlay's silent fallback video |
| `lite` | 60 fps H.264/AAC feed for one OBS client |
| `web` | Shared video/audio encoder and browser player |
| `overlay` | Control page, stats overlays, and requests to the supervisor |

The `cogbench-stream` service exposes these ports:

| Node port | Container port | Use |
|---|---|---|
| 31720 | 8080 | Controls, overlays, and silent fallback video |
| 31723 | 8092 | OBS A/V at `/stream.ts` |
| 31724 | 8094 | Browser watch page and A/V |

The command daemon (8765) and supervisor (8099) are not exposed by this service.
`just game look` runs the client inside the game container. Stop the agent with
`just stop` before taking manual control; see [game commands](../AGENT.md).

## Persistent data

The existing PVC **`cogbench-data`** is mounted at `/data`:

| Path | Contents |
|---|---|
| `/data/game` | Your owned game files and patched `SDL.dll` |
| `/data/profile` | Game profile, saves, dumps, and score history |
| `/data/stream` | Overlay state, saved run settings, and run results |
| `/data/wine.log`, `/data/runner.log`, `/data/pulse.log` | Game, supervisor, and audio logs |

The X display socket, audio socket, and fallback frames use temporary shared
volumes. They are recreated with the pod. Application code is baked into the
image under `/opt/cogbench`; use the image build for code updates.

## Update code on the existing worker

1. Run `just changes` and review the intended changes.
2. Run `just check` for local regression and syntax checks.
3. Run `just build` if you want to build and push without interrupting play.
4. When ready to interrupt the worker, run `just deploy`. It builds, pushes,
   updates image references in `deployment.yaml`, applies that manifest, and
   waits for the rollout. An optional tag works with either command:
   `just deploy my-tag`.
5. Run `just doctor`, `just status`, and `just stream-status`. Reopen the watch
   page, click **Sound on**, and check picture and sound. Check OBS separately.
   Restart `just connect` if you were using a tunnel.

`just build` and `just deploy` each build an image; deploy is not a promotion of
an earlier build. The script applies **only `deployment.yaml`**, not the egress
policy. It does not create the namespace, PVC, endpoint Secret, or game files.

For recovery after a bad rollout, first inspect rollout history and choose a
known-good revision. These commands affect the whole worker:

```sh
kubectl --context admin@dev-cluster -n cogbench rollout history deployment/cogbench
# Replace REVISION with the known-good revision number before running:
kubectl --context admin@dev-cluster -n cogbench rollout undo deployment/cogbench --to-revision=REVISION
kubectl --context admin@dev-cluster -n cogbench rollout status deployment/cogbench --timeout=400s
```

Rollback restores the pod template, not the PVC or the local manifest. Align the
local image references before the next apply so it does not redeploy the bad tag.

## Moving to another cluster or machine

This directory updates an existing installation. It is **not a fresh-cluster
installer**. Before moving it, provide or review:

- Namespace `cogbench` and a compatible `cogbench-data` PVC containing your game
  and test profile. This checkout has no `stage-game.sh`, despite an older error
  message referring to one.
- Secret `cogbench-endpoint`, keys `url` and `api_key`. Reuse the configured
  secret-management process; credentials do not belong in docs or shell history.
- Registry/base-image access and a BuildKit worker. The Dockerfile builds on an
  existing private base image rather than constructing the game runtime afresh.
- CPU compatibility. FFmpeg is compiled with `-march=native`; review the builder
  and destination CPU together. The deployment currently has no node selector.
- Permission for the game container's `SYS_PTRACE` capability. The other
  containers drop all capabilities.
- Resource capacity. Containers have requests but no CPU/memory limits; check
  node pressure when sharing the worker with other workloads.
- Service ports and network policies appropriate to the destination network.

The shortcuts default to context `admin@dev-cluster`, namespace `cogbench`, and
host `slacker.local`. Set `COGBENCH_CONTEXT` and `COGBENCH_HOST` for another
installation. Deployment is explicitly restricted to namespace `cogbench` by
the build script; changing the shortcut namespace does not rewrite the manifest.
See [setup](../SETUP.md#build-or-update-the-existing-worker) for build overrides.

## Model connectivity and privacy

[netpol-model-egress.yaml](netpol-model-egress.yaml) allows cluster DNS, public
IPv4 TCP 443 and 6697 (Twitch IRC), and one explicit LAN model endpoint:
`192.168.1.120:8000`. If that machine's DHCP address changes, review both the
endpoint configuration and this exception. A running game with failed model
requests can simply be a stale endpoint address.

Policies apply to the whole pod, including the game. The public HTTPS rule is
not restricted to one model hostname. Other policies can add access; inspect
the cluster's actual policies before making isolation claims:

```sh
kubectl --context admin@dev-cluster -n cogbench get networkpolicy
```

After reviewing a deliberate policy change, apply it separately:

```sh
kubectl --context admin@dev-cluster -n cogbench apply -f harness/k8s/netpol-model-egress.yaml
```

That path assumes you are in the parent `cogbench` folder; from `harness`, use
`k8s/netpol-model-egress.yaml`.

The entrypoint removes `discord.txt` from the game/profile directories and
initializes the test profile. Keep score uploads disabled when changing game
configuration. These are application settings, not a guarantee supplied by the
egress policy.

This guide describes the checked-in configuration. Use the checks above to
establish the state of the running worker.
