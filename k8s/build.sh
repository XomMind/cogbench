#!/bin/sh
# Build the harness image and roll it out.
#
# The build context needs a copy of the harness next to the Dockerfile, because
# BuildKit cannot reach outside it. That copy is staged here rather than kept in
# the repository: it is two megabytes of duplicate source, and a duplicate that
# is edited by hand drifts -- during one session the same file was copied in
# four separate times and forgotten twice, which deploys an image that silently
# lacks the fix you just made.
#
#   ./build.sh                  build, push and roll out a timestamped tag
#   ./build.sh mytag            the same, with a tag you choose
#   COGBENCH_NO_ROLLOUT=1 ...   build and push only
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
ctx="$here/image"
tag=${1:-$(date +%Y%m%d-%H%M%S)}
image=${COGBENCH_IMAGE:-192.168.1.187:30500/cogbench}:$tag
context=${COGBENCH_CONTEXT:-$(kubectl config current-context)}
build_context=${COGBENCH_BUILD_CONTEXT:-$context}
build_namespace=${COGBENCH_BUILD_NAMESPACE:-buildkit}
if [ "${COGBENCH_NO_ROLLOUT:-0}" = "0" ] && [ "${COGBENCH_NAMESPACE:-cogbench}" != "cogbench" ]; then
  echo "deployment.yaml targets namespace cogbench; refusing a different COGBENCH_NAMESPACE" >&2
  exit 2
fi
buildkit=${COGBENCH_BUILDKIT:-}
if [ -z "$buildkit" ]; then
  # Resolve the current builder, not a pod name that expires on its next rollout.
  pod=$(kubectl --context "$build_context" --request-timeout=10s -n "$build_namespace" \
    get pods -l "${COGBENCH_BUILD_SELECTOR:-app=slacker0}" -o json | python3 -c '
import json, sys
pods = [p["metadata"]["name"] for p in json.load(sys.stdin)["items"]
        if not p["metadata"].get("deletionTimestamp")
        and any(c["type"] == "Ready" and c["status"] == "True"
                for c in p.get("status", {}).get("conditions", []))]
if len(pods) != 1:
    sys.exit("Expected one ready BuildKit pod, found %d; check COGBENCH_BUILD_SELECTOR" % len(pods))
print(pods[0])')
  buildkit=$(python3 - "$pod" "$build_context" "$build_namespace" <<'PYURL'
import sys, urllib.parse
print("kube-pod://" + sys.argv[1] + "?" + urllib.parse.urlencode({
    "context": sys.argv[2], "namespace": sys.argv[3], "container": "buildkitd"}))
PYURL
  )
fi
printf 'Build context: %s; workload context: %s\n' "$build_context" "$context"

# Everything the image runs. Listed explicitly rather than copying the whole
# tree: the harness directory also holds model weights, traces and a cog-minder
# clone, none of which belong in a container image.
files="agent.py bot.py botdex.py cogbench.py episode.py glyphs.py hackdex.py
       itemdex.py runner.py statdump.py stream.py tracemodel.py twitch.py
       actions.json"

rm -rf "$ctx/harness"
mkdir -p "$ctx/harness/stream"
for f in $files; do
  cp "$root/$f" "$ctx/harness/"
done
cp "$root"/webstream.py "$ctx/harness/"
cp "$root"/stream/*.html "$ctx/harness/stream/"

# The dexes read cog-minder's JSON at the path it sits at in the working tree,
# so the layout has to be reproduced -- but only these three files. The clone
# itself is 117MB of repository and the three that matter are 1.5MB. Leaving
# them out is not a subtle failure: Botdex() raises in Agent.__init__ and every
# run dies two seconds in, which is how this was found.
mkdir -p "$ctx/harness/cog-minder/src/json"
for j in bots.json items.json machine_hacks.json; do
  cp "$root/cog-minder/src/json/$j" "$ctx/harness/cog-minder/src/json/"
done

# The reader is compiled in the image now, so its source has to reach the build
# context too. Only the crate: no target/, which is gigabytes of build output.
sm=$(cd "$root/../StatMind" && pwd)
rm -rf "$ctx/statmind"
mkdir -p "$ctx/statmind"
cp "$sm/Cargo.toml" "$sm/Cargo.lock" "$sm/build.rs" "$ctx/statmind/"
cp -R "$sm/src" "$ctx/statmind/src"

# ...and so does SDL-1.2, which this used to exclude on the grounds that "the
# shim is a Windows DLL, built separately". It is still a Windows DLL; building
# it separately is what went wrong. The reader above and the shim exchange
# StatmindLuigiStatus by memory layout, so they are one artifact in two files,
# and the only way to keep them in step is to build them from the same tree in
# the same place. The hand-built DLL staged onto the PVC fell months behind:
# its struct ended at 28 bytes where the reader read 44, so `map_object` came
# back as the neighbouring census magic and every cell read failed with EFAULT.
#
# The cost is the context: ~8MB, nearly all of it src/, against 2.8MB before.
# That is a second or two to an in-cluster BuildKit and only when a file
# changes, which is worth paying to make the drift unrepresentable. The
# exclusions below are what keep it to 8MB rather than 17MB.
#
#   build-win32/  the maintainer's out-of-tree build. Object files from another
#                 compiler and a Makefile full of /Users paths; it would also
#                 hand the image stale .lo files to skip recompiling.
#   configure,    generated, and gitignored precisely because they are. The
#   aclocal.m4    image regenerates them with its own autoconf, so what ships
#                 is never an artifact someone happened to have lying about.
#   docs/ test/   1.8MB of HTML and a test suite; configure builds neither.
#   *.zip *.bin   Borland/Watcom/Symbian/CodeWarrior project archives, ~900KB
#                 of build systems for platforms that are not this one.
sdl=$sm/SDL-1.2
[ -f "$sdl/configure.in" ] || {
  echo "no SDL sources at $sdl -- run: git -C $sm submodule update --init" >&2
  exit 1
}
rm -rf "$ctx/sdl"
mkdir -p "$ctx/sdl"
for d in src include build-scripts acinclude; do
  cp -R "$sdl/$d" "$ctx/sdl/$d"
done
# configure.in plus the templates AC_CONFIG_FILES names. SDL.qpg.in and
# SDL.spec.in build nothing, but configure substitutes all five and dies on a
# missing one. README is not documentation here: it is the file
# AC_CONFIG_SRCDIR names, so configure uses it to decide it is looking at an
# SDL tree at all, and without it stops at "cannot find sources (README)".
for f in README configure.in Makefile.in sdl-config.in sdl.pc.in SDL.qpg.in SDL.spec.in; do
  cp "$sdl/$f" "$ctx/sdl/"
done
# Belt and braces: cp -R above follows the working tree, and a maintainer who
# has ever run build-sdl.sh in-tree rather than out-of-tree would otherwise
# ship object files into the context.
find "$ctx/sdl" \( -name '*.o' -o -name '*.lo' -o -name '*.a' -o -name '*.la' \
  -o -name '.libs' -o -name '.deps' \) -exec rm -rf {} + 2>/dev/null || true

echo "building $image"
buildctl --addr "$buildkit" build \
  --frontend dockerfile.v0 \
  --local context="$ctx" --local dockerfile="$ctx" \
  --output "type=image,name=$image,push=true,registry.insecure=true"

if [ "${COGBENCH_NO_ROLLOUT:-0}" != "0" ]; then
  echo "built $image (not rolled out)"
  exit 0
fi

# The tag in the manifest is rewritten rather than templated, so the file in
# git always records exactly what is deployed.
python3 - "$here/deployment.yaml" "$image" <<'PY'
import re, sys
path, image = sys.argv[1], sys.argv[2]
s = open(path).read()
new = re.sub(r"(?m)^(\s+image:\s+)\S+$", lambda m: m.group(1) + image, s)
if new != s:
    open(path, "w").write(new)
    print("deployment.yaml -> %s" % image)
PY

kubectl --context "$context" apply -f "$here/deployment.yaml"
kubectl --context "$context" -n cogbench rollout status deploy/cogbench --timeout=400s
