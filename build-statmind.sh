#!/usr/bin/env bash
# Build the StatMind reader and re-sign it in one step.
#
# The signature is not optional and not sticky: task_for_pid() needs
# com.apple.security.cs.debugger, and *every* cargo build replaces the binary
# and drops the entitlement with it. A rebuild without a re-sign fails at
# attach time with "Failed to get task port for PID <n>: 5", which reads like a
# permissions problem with the game rather than a stale build -- so the two
# steps live together here.
set -euo pipefail

REPO="${REPO:-/Users/heni/genAI/cogbench/StatMind}"
IDENT="${STATMIND_CODESIGN_ID:-9E4DDC0A250D30CB8BEB148C8F5EDC283610D680}"
PROFILE_DIR="${PROFILE_DIR:-release}"
BIN="$REPO/target/$PROFILE_DIR/statmind"

cd "$REPO"
if [ "$PROFILE_DIR" = "release" ]; then
  cargo build --release
else
  cargo build
fi

codesign --entitlements "$REPO/entitlements.xml" -fs "$IDENT" "$BIN"
echo "==> built and signed: $BIN"
codesign -d --entitlements - "$BIN" 2>&1 | grep -q debugger \
  && echo "==> debugger entitlement present" \
  || { echo "!! debugger entitlement MISSING -- task_for_pid will fail" >&2; exit 1; }
