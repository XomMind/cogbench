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
buildkit=${COGBENCH_BUILDKIT:-kube-pod://slacker0-587575666b-2n6k8?namespace=buildkit&container=buildkitd}

# Everything the image runs. Listed explicitly rather than copying the whole
# tree: the harness directory also holds model weights, traces and a cog-minder
# clone, none of which belong in a container image.
files="agent.py bot.py botdex.py cogbench.py episode.py glyphs.py hackdex.py
       itemdex.py runner.py statdump.py stream.py tracemodel.py twitch.py
       actions.json"

rm -rf "$ctx/harness"
mkdir -p "$ctx/harness/stream"
for f in $files; do
  [ -e "$root/$f" ] && cp "$root/$f" "$ctx/harness/" || true
done
cp "$root"/webstream.py "$ctx/harness/" 2>/dev/null || true
cp "$root"/stream/*.html "$ctx/harness/stream/"
[ -d "$root/data" ] && cp -R "$root/data" "$ctx/harness/" 2>/dev/null || true

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

kubectl apply -f "$here/deployment.yaml"
kubectl -n cogbench rollout status deploy/cogbench --timeout=400s
