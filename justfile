set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]

export COGBENCH_ROOT := source_directory()
export COGBENCH_CONTEXT := env("COGBENCH_CONTEXT", "admin@dev-cluster")
export COGBENCH_NAMESPACE := env("COGBENCH_NAMESPACE", "cogbench")
export COGBENCH_HOST := env("COGBENCH_HOST", "slacker.local")
export COGBENCH_CONTROL_URL := env("COGBENCH_CONTROL_URL", "http://" + COGBENCH_HOST + ":31720")
export COGBENCH_WATCH_URL := env("COGBENCH_WATCH_URL", "http://" + COGBENCH_HOST + ":31724")
export COGBENCH_OBS_URL := env("COGBENCH_OBS_URL", "http://" + COGBENCH_HOST + ":31723/stream.ts")

# Show the laptop command menu. Nothing starts automatically.
[default]
help:
    @printf '\nCogbench · laptop → %s · context %s\n\n' "$COGBENCH_HOST" "$COGBENCH_CONTEXT"
    @just --justfile "$COGBENCH_ROOT/justfile" --list --list-heading 'Choose a command:\n'
    @printf '\nFirst visit: just doctor   Then: just watch or just control\nTunnel fallback: just connect (leave running in another terminal)\n'

# Open the live game in your default macOS browser.
[group('Watch')]
watch:
    @open "$COGBENCH_WATCH_URL/"

# Open run controls, model selection, chat, and scene settings.
[group('Watch')]
control:
    @open "$COGBENCH_CONTROL_URL/control.html"

# Open the OBS overlay; layout is desktop or vertical.
[group('Watch')]
overlay layout="desktop":
    @case "$1" in desktop|vertical) open "$COGBENCH_CONTROL_URL/overlay.html?layout=$1" ;; *) echo 'Use: just overlay [desktop|vertical]' >&2; exit 2 ;; esac

# Print the current browser, controls, overlay, and OBS source URLs.
[group('Watch')]
urls:
    @printf 'Watch    %s/\nControls %s/control.html\nOverlay  %s/overlay.html\nOBS A/V  %s\n' "$COGBENCH_WATCH_URL" "$COGBENCH_CONTROL_URL" "$COGBENCH_CONTROL_URL" "$COGBENCH_OBS_URL"

# Copy the OBS A/V URL to the macOS clipboard.
[group('Watch')]
obs-url:
    @printf '%s' "$COGBENCH_OBS_URL" | pbcopy
    @printf 'Copied OBS Media Source URL: %s\n' "$COGBENCH_OBS_URL"

# Check laptop tools, the selected cluster, and both live HTTP services.
[group('Inspect')]
doctor:
    #!/usr/bin/env bash
    set -euo pipefail
    failed=0
    for tool in python3 kubectl curl open; do
      if command -v "$tool" >/dev/null; then printf 'OK   %s\n' "$tool"; else printf 'MISS %s\n' "$tool"; failed=1; fi
    done
    for tool in node ffmpeg buildctl black ruff mypy; do
      command -v "$tool" >/dev/null || printf 'NOTE %s missing (needed for checks or builds only)\n' "$tool"
    done
    printf '\nCluster: %s / %s\n' "$COGBENCH_CONTEXT" "$COGBENCH_NAMESPACE"
    kubectl --context "$COGBENCH_CONTEXT" --request-timeout=10s -n "$COGBENCH_NAMESPACE" get pods -l app=cogbench -o wide || failed=1
    for url in "$COGBENCH_CONTROL_URL/run/status" "$COGBENCH_WATCH_URL/stat"; do
      if curl --fail --silent --show-error --connect-timeout 3 --max-time 10 "$url" >/dev/null; then printf 'OK   %s\n' "$url"; else failed=1; fi
    done
    if [ "$failed" -ne 0 ]; then printf '\nCheck VPN/LAN access or use just connect. For another cluster, set COGBENCH_CONTEXT.\n' >&2; fi
    exit "$failed"

# Read the agent's status, last outcome, and saved run settings.
[group('Inspect')]
status:
    @just --justfile "$COGBENCH_ROOT/justfile" _api GET /run/status

# List models available through the worker's configured endpoint.
[group('Inspect')]
models:
    @just --justfile "$COGBENCH_ROOT/justfile" _api GET /models

# Show the encoder's health and viewer count.
[group('Inspect')]
stream-status:
    @curl --fail --silent --show-error --connect-timeout 3 --max-time 10 "$COGBENCH_WATCH_URL/stat" | python3 -m json.tool

# Show the last agent log lines, without attaching to the game.
[group('Inspect')]
logs lines="40":
    @just --justfile "$COGBENCH_ROOT/justfile" _api GET /run/log "n=$1"

# Start on the current episode; omitted arguments retain saved settings.
[group('Run')]
start decisions="" policy="":
    @just --justfile "$COGBENCH_ROOT/justfile" _api POST /run/start "decisions=$1" "policy=$2"

# Stop the agent and cancel automatic revival; keep the game open.
[group('Run')]
stop:
    @just --justfile "$COGBENCH_ROOT/justfile" _api POST /run/stop

# Restart only the agent on the same episode, with its saved settings.
[group('Run')]
restart:
    @just --justfile "$COGBENCH_ROOT/justfile" _api POST /run/restart

# Switch the live model; use an exact ID from just models.
[group('Run')]
model name:
    @just --justfile "$COGBENCH_ROOT/justfile" _api POST /params "model=$1"

# Enable or disable automatic new episodes after a game ends.
[group('Run')]
loop enabled="on":
    @just --justfile "$COGBENCH_ROOT/justfile" _api POST /run/spec "loop=$1"

# Enable or disable automatic recovery after an agent crash.
[group('Run')]
revive enabled="on":
    @just --justfile "$COGBENCH_ROOT/justfile" _api POST /run/spec "supervise=$1"

# List known Kubernetes contexts without switching the laptop's current one.
[group('Cluster')]
contexts:
    @kubectl config get-contexts

# Inspect the worker deployment, pods, and exposed services.
[group('Cluster')]
pods:
    @kubectl --context "$COGBENCH_CONTEXT" --request-timeout=10s -n "$COGBENCH_NAMESPACE" get deployment,pods,services -l app=cogbench -o wide

# Forward web/control/OBS ports to localhost; Ctrl-C closes this tunnel only.
[group('Cluster')]
connect:
    @printf 'In another terminal: just local watch\n                    just local control\n'
    @kubectl --context "$COGBENCH_CONTEXT" -n "$COGBENCH_NAMESPACE" port-forward --address 127.0.0.1 service/cogbench-stream 31720:8080 31723:8092 31724:8094

# Use the localhost tunnel: just local watch, local control, or local status.
[group('Cluster')]
local *args:
    @COGBENCH_HOST=127.0.0.1 COGBENCH_CONTROL_URL=http://127.0.0.1:31720 COGBENCH_WATCH_URL=http://127.0.0.1:31724 COGBENCH_OBS_URL=http://127.0.0.1:31723/stream.ts just --justfile "$COGBENCH_ROOT/justfile" "$@"

# Follow a container's logs; Ctrl-C stops following. Default: game container.
[group('Cluster')]
worker-logs container="cogbench":
    @kubectl --context "$COGBENCH_CONTEXT" -n "$COGBENCH_NAMESPACE" logs -f deployment/cogbench -c "$1" --tail=80

# Open an interactive shell inside a worker container.
[group('Cluster')]
shell container="cogbench":
    @kubectl --context "$COGBENCH_CONTEXT" -n "$COGBENCH_NAMESPACE" exec -it deployment/cogbench -c "$1" -- /bin/sh

# Send a game command via the worker's existing daemon (may consume turns).
[group('Cluster')]
game +args:
    @kubectl --context "$COGBENCH_CONTEXT" -n "$COGBENCH_NAMESPACE" exec deployment/cogbench -c cogbench -- python3 /opt/cogbench/cogbench.py "$@"

# Run the local regression suite. No model, game, or cluster changes.
[group('Develop')]
test:
    @python3 -m unittest discover -s "$COGBENCH_ROOT/tests" -v
    @node "$COGBENCH_ROOT/tests/test_watch.cjs"

# Reformat the Python sources in place with black.
[group('Develop')]
fmt:
    @black "$COGBENCH_ROOT" "$COGBENCH_ROOT/tests"

# Report lint, type and formatting problems without changing any file.
[group('Develop')]
lint:
    @ruff check "$COGBENCH_ROOT" "$COGBENCH_ROOT/tests"
    @mypy "$COGBENCH_ROOT"/*.py "$COGBENCH_ROOT"/tests/*.py
    @black --check "$COGBENCH_ROOT" "$COGBENCH_ROOT/tests"

# Run tests, lint, types, formatting, and shell and whitespace checks.
[group('Develop')]
check: test lint
    @python3 -m compileall -q "$COGBENCH_ROOT"/*.py
    @bash -n "$COGBENCH_ROOT/k8s/build.sh"
    @sh -n "$COGBENCH_ROOT/k8s/image/cogbench-entrypoint"
    @git -C "$COGBENCH_ROOT" diff --check

# Build and push on slacker's BuildKit; leave the running worker untouched.
[group('Develop')]
build tag="":
    @COGBENCH_NO_ROLLOUT=1 "$COGBENCH_ROOT/k8s/build.sh" "$1"

# Build, push, and roll out code. This restarts the game and stream containers.
[group('Develop')]
deploy tag="":
    @COGBENCH_NO_ROLLOUT=0 "$COGBENCH_ROOT/k8s/build.sh" "$1"

# Display the changes waiting in both source repositories.
[group('Develop')]
changes:
    @git -C "$COGBENCH_ROOT" status --short
    @git -C "$COGBENCH_ROOT/../StatMind" status --short

[private]
_api method path *fields:
    #!/usr/bin/env python3
    import json, os, sys, urllib.error, urllib.parse, urllib.request
    method, path, *fields = sys.argv[1:]
    params = {}
    for field in fields:
        key, value = field.split('=', 1)
        if not value:
            continue
        if key in ('loop', 'supervise'):
            if value.lower() not in ('on', 'off', 'true', 'false', '1', '0'):
                sys.exit('Use on or off for ' + key)
            value = 'true' if value.lower() in ('on', 'true', '1') else 'false'
        if key in ('decisions', 'n'):
            if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 100000:
                sys.exit(key + ' must be a positive integer no larger than 100000')
        if key == 'policy' and value not in ('model', 'heuristic', 'scripts'):
            sys.exit('Policy must be model, heuristic, or scripts')
        params[key] = value
    url = os.environ['COGBENCH_CONTROL_URL'].rstrip('/') + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    try:
        request = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(request, timeout=35) as response:
            data = json.load(response)
    except (OSError, ValueError) as exc:
        sys.exit('Worker request failed: %s\nCheck just doctor, or use just connect.' % exc)
    if 'lines' in data:
        print('\n'.join(data['lines']))
    else:
        print(json.dumps(data, indent=2))
    if data.get('ok') is False or data.get('error'):
        sys.exit(1)
