# Connect to Cogbench

For the existing Kubernetes worker, you only need network access and a browser.
[Watch the game](http://slacker.local:31724/) or
[open the controls](http://slacker.local:31720/control.html).
See [OBS setup](OBS.md) to stream it.

## Laptop shortcuts

The `justfile` provides shortcuts for macOS. From the `cogbench` or `harness`
folder, run:

```sh
just             # show the menu
just doctor      # check tools, cluster access, and web services
just watch
just control
```

Shortcuts need `just`; the health check also uses Python 3, `kubectl`, `curl`,
and macOS `open`. Cluster commands need working Kubernetes credentials.
Watching a reachable browser URL does not require these tools.

Defaults: host `slacker.local`, Kubernetes context `admin@dev-cluster`, namespace
`cogbench`. Override them when needed:

```sh
COGBENCH_HOST=worker.example just urls
COGBENCH_CONTEXT=my-context just doctor
```

You can also set `COGBENCH_CONTROL_URL`, `COGBENCH_WATCH_URL`, and
`COGBENCH_OBS_URL` individually. `just contexts` lists your Kubernetes contexts
without changing the active one.

## Connection problems

1. Check that your laptop is on the worker's LAN or VPN.
2. Run `just doctor`. It checks the configured cluster and HTTP services.
3. If the worker hostname is unreachable but Kubernetes access works, run:

   ```sh
   just connect
   ```

   Leave that terminal open. In another terminal:

   ```sh
   COGBENCH_HOST=127.0.0.1 just watch
   COGBENCH_HOST=127.0.0.1 just control
   COGBENCH_HOST=127.0.0.1 just obs-url
   ```

   In OBS overlay URLs, replace `slacker.local` with `127.0.0.1` too.
4. If the tunnel stops after a worker restart, run `just connect` again.
   Ctrl-C closes the tunnel without stopping the game.

The tunnel forwards controls/overlays (31720), OBS A/V (31723), and browser
video (31724). It does not forward the retired Matroska feed.

## Check a stopped agent

```sh
just status
just logs
just stream-status
```

Use the control page to inspect the model and decision budget. **Start** resumes
agent play on the current episode; **Restart** restarts the agent on that same
episode. See [the result table](README.md#everyday-controls) for outcome meanings.

For worker problems, `just pods` shows deployment status and `just worker-logs`
follows the game container's logs. Ctrl-C stops following logs only.

## Build or update the existing worker

See the [Kubernetes worker guide](k8s/README.md) for container logs, persistent
data, deployment recovery, model networking, and moving to another cluster.

This is maintenance, not a prerequisite for watching. The current image build
requires the existing base image, registry, BuildKit worker, and game data; it
is not a self-contained fresh-cluster installer. Cogmind is not bundled here.

```sh
just check       # local tests and checks
just build       # build and push an image; leave the running worker alone
just deploy      # build, push, and roll out: RESTARTS game and stream containers
```

`just deploy` changes the image references in `k8s/deployment.yaml`. Review the
manifest and pending changes first. Builds use the selected Kubernetes context;
`COGBENCH_BUILD_CONTEXT` can select a separate builder context. The build script
also accepts `COGBENCH_IMAGE`, `COGBENCH_BUILDKIT`, `COGBENCH_BUILD_NAMESPACE`,
and `COGBENCH_BUILD_SELECTOR`. See [build.sh](k8s/build.sh) for their defaults.

The manifest targets namespace `cogbench`. The FFmpeg image is compiled for the
builder's CPU; moving to different hardware needs a build configuration review.

## Supported game builds

The shim writes to the game's memory and calls into it, so it runs only against
executables it recognises. Two Beta 17.1 builds are supported; they share a
version string but their code sits at different addresses, and each is pinned to
its own set of instruction fingerprints in
[`statmind_build.h`](../StatMind/SDL-1.2/src/statmind_build.h). Anything else is
refused, and the game then runs normally with LuigiAI and stat dumps off.

Check an executable without launching anything:

```sh
python3 verify_retail.py "path/to/COGMIND (Beta 17.1)/COGMIND.exe"
```

Keep each new release unmodified under `releases/<version>-<sha256 prefix>/`.
[The build notes](notes/retail-17.1.md) record what the fingerprints pin, what
was compared between the two builds, and the steps for adding a third.

## Local macOS bridge development

Use this only when working on the bridge itself. Everyday operation uses the
worker. Local development needs an owned, supported Cogmind installation, Wine,
Rust, the patched SDL source, and the SDL cross-build tools.

From `harness`, with `StatMind` beside it:

```sh
git -C ../StatMind submodule update --init SDL-1.2
touch ../StatMind/SDL-1.2/src/SDL.c
SDL_DIR="$(cd ../StatMind/SDL-1.2 && pwd)" ./build-sdl.sh
REPO="$(cd ../StatMind && pwd)" STATMIND_CODESIGN_ID=- ./build-statmind.sh
```

Touching `SDL.c` forces a rebuild after shim-header edits. The StatMind helper
builds and signs the reader together; rebuilding without signing loses the
macOS debugger entitlement.

Back up the stock `SDL.dll` before installing the built DLL into an isolated
Cogmind test installation. `stage-test-install.sh` does both, copying the newest
ingested release and a separate test profile rather than touching the live game
or a personal save; it refuses any executable the shim does not recognise:

```sh
SDL_DLL=../StatMind/SDL-1.2/build-win32/build/.libs/SDL.dll ./stage-test-install.sh
```

Wrapper-specific details are preserved in
[the historical macOS setup log](notes/setup-history.md); review its paths
before using any commands. Its old verification labels are historical.

Once that game is running with the patched DLL:

```sh
export STATMIND_BIN="$(cd ../StatMind && pwd)/target/release/statmind"
python3 cogbench.py daemon   # leave running
```

In another terminal in `harness`, use `python3 cogbench.py look` or follow
[the game command guide](AGENT.md).

## Research and limitations

`look` reads ground-truth cells, including information outside the player's
view. Use `dump` for the game's explored-map report; its map is a 50×50 window,
not the whole floor. Use `stats.exploration.turnsPassed` in the raw stat dump
for elapsed turns; `actionReady` is not a reliable elapsed-turn counter.

The [research notebook](notes/b17.1-luigiai.md) preserves discoveries, failed
experiments, addresses, and measured results. Later rounds can supersede earlier
ones. It is a record of development, not the everyday setup procedure.
