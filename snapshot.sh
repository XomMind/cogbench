#!/usr/bin/env bash
# Dump every address the harness depends on, so a before/after pair can be diffed
# across a map transition. Answers: which of these are actually stable?
#
#   ./snapshot.sh 250 228 > /tmp/before.txt     # integrity, matter from the HUD
#   ...walk up the stairs...
#   ./snapshot.sh 250 228 > /tmp/after.txt
#   diff /tmp/before.txt /tmp/after.txt
set -euo pipefail
INTEG="${1:?usage: snapshot.sh <integrity> <matter>}"
MATTER="${2:?usage: snapshot.sh <integrity> <matter>}"
SM="${STATMIND_BIN:-/Users/heni/genAI/cogbench/StatMind/target/release/statmind}"

REQ=$(mktemp); OUT=$(mktemp)
cat > "$REQ" <<JSON
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"luigi_raw","arguments":{}}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"map_header","arguments":{}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"player","arguments":{}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"find_stats","arguments":{"integrity":$INTEG,"matter":$MATTER}}}
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"fov","arguments":{"limit":8}}}
JSON
timeout 300 "$SM" --mcp < "$REQ" > "$OUT" 2>/dev/null

python3 - "$OUT" <<'PY'
import json, sys
res={}
for line in open(sys.argv[1]):
    d=json.loads(line); i=d.get('id')
    if i in (None,1): continue
    if 'error' in d: res[i]={"_error": d['error']['message'][:200]}; continue
    res[i]=json.loads(d['result']['content'][0]['text'])

def g(i,k,default="?"):
    v=res.get(i,{})
    return v.get(k,default) if isinstance(v,dict) else default

print("---- STATIC (expected identical across maps) ----")
print(f"  LuigiAi base           0x00CEBFFC   magics_ok={g(2,'magic1_ok')}")
print(f"  player record          0x00D2D338   plausible={g(4,'plausible')}")
print(f"  map object             {g(3,'map_obj')}")
print()
print("---- PER-MAP (the question) ----")
print(f"  map dims               {g(3,'width')} x {g(3,'height')}")
print(f"  cells_base             {g(3,'cells_base')}")
print(f"  LuigiAi.mapData        {g(2,'map_data')}")
print(f"  fov_obj                0x{g(6,'fov_obj',0):08X}" if isinstance(g(6,'fov_obj',0),int) else f"  fov_obj                {g(6,'fov_obj')}")
st=res.get(5,{})
if "_error" in st:
    print(f"  stat block             ERROR: {st['_error']}")
else:
    print(f"  stat block matches     {st.get('matches')}")
    for s in st.get('stats',[]):
        print(f"    base {s['base']}  integrity={s['integrity']} energy={s['energy']} "
              f"matter={s['matter']} heat={s['heat']} corruption={s['corruption']} speed={s['speed']}")
print()
print("---- GAME STATE ----")
print(f"  location               depth={g(2,'location_depth')} map_type={g(2,'location_map')}")
print(f"  player pos             ({g(4,'x')},{g(4,'y')})  handle=0x{g(4,'handle',0):08X}  {g(4,'entity_name')}")
print(f"  LuigiAi.actionReady    {g(2,'action_ready')}")
print(f"  LuigiAi.player         {g(2,'player')}")
print(f"  mapCursorIndex         {g(2,'map_cursor_index')}")
v=res.get(6,{}).get('visible',{})
print(f"  fov visible container  len={v.get('len')} first={v.get('first')} plausible={v.get('plausible')}")
PY
rm -f "$REQ" "$OUT"
