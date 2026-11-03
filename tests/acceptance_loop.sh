#!/bin/bash
# PLAN A7 step 7: job acceptance test on loop devices. Needs root (losetup); touches only its own image files.
#   sudo tests/acceptance_loop.sh [path/to/nandwash]
# 1. check + refresh jobs on 3 drives in the foreground
# 2. a background job killed with SIGKILL -> interrupted -> resumed
# 3. a background job stopped with SIGTERM -> paused at a checkpoint -> resumed from it, gap-free latency map
# 4. cancel
set -euo pipefail
NW=$(command -v "${1:-nandwash}")
PY=$(dirname "$NW")/python3; [ -x "$PY" ] || PY=python3     # the interpreter nandwash is installed in
W=$(mktemp -d /var/tmp/nw-accept.XXXXXX)
export NANDWASH_TEST_LOOP=1 NANDWASH_STATE=$W/state NANDWASH_LOCK=$W/lock NANDWASH_NO_IMPORT=1
LOOPS=()
cleanup() {
  for j in "$W"/state/jobs/*/; do                  # only this test's jobs, never a real one
    [ -d "$j" ] && systemctl stop "nandwash-job-$(basename "$j")" 2>/dev/null || true
  done
  for l in "${LOOPS[@]}"; do losetup -d "$l" 2>/dev/null || true; done
  rm -rf -- "$W"
}
trap cleanup EXIT
fail() { echo "FAIL: $*"; exit 1; }
# A job's exit code is its worst drive's verdict (0 healthy/improved, 1 slow/partly, 2 inconclusive, 3 errors);
# blank loop images on a busy host can land anywhere in 0-2. Anything else (a crash, 130 paused) fails.
runok() { local rc=0; "$@" || rc=$?; [ "$rc" -le 2 ] || { echo "exit $rc"; return 1; }; }
js() { "$NW" status --json "$@" | python3 -c 'import json,sys; j=json.load(sys.stdin); print(eval(sys.argv[1]))' "$EXPR"; }

for i in 1 2 3; do truncate -s 128M "$W/d$i.img"; LOOPS+=("$(losetup --show -f "$W/d$i.img")"); done
truncate -s 80G "$W/big.img"; LOOPS+=("$(losetup --show -f "$W/big.img")")     # sparse: reads cost nothing
small=$(printf '%s,' "${LOOPS[@]:0:3}" | sed 's|/dev/||g; s|,$||'); big=$(basename "${LOOPS[3]}")
echo "drives: $small  big: $big"

echo "== 1. foreground check + refresh"
runok "$NW" run --drives "$small" --goal check --on-result "cat > $W/onresult.json" > "$W/1.out" 2>&1 || fail "check job: $(tail -3 "$W/1.out")"
EXPR='[d["status"] for d in j["drives"]]'; [ "$(js)" = "['done', 'done', 'done']" ] || fail "check: $(js)"
"$PY" - "$W/onresult.json" <<'PYCHK' || fail "--on-result got no results JSON"
import json, sys
j = json.load(open(sys.argv[1]))
assert j["nandwash_result"] == 1 and len(j["drives"]) == 3 and j["job_state"] == "done", j
print("   on-result:", [(d["id"], d["state"]) for d in j["drives"]], "exit", j["exit"])
PYCHK
runok "$NW" run --drives "$small" --goal refresh --yes > "$W/2.out" 2>&1 || fail "refresh job: $(tail -3 "$W/2.out")"
EXPR='[d["steps"][-1] + ":" + d["status"] for d in j["drives"]]'
[ "$(js)" = "['rewrite-chunks:done', 'rewrite-chunks:done', 'rewrite-chunks:done']" ] || fail "refresh: $(js)"
"$NW" results > "$W/res.out" || fail "results list"
[ "$(grep -c 'nw-' "$W/res.out")" = 3 ] || fail "results list: $(cat "$W/res.out")"
one=$(grep -o 'nw-[0-9a-f]*' "$W/res.out" | head -1)
"$NW" results "$one" --history > "$W/card.out" || fail "results card"
grep -q "STATE" "$W/card.out" && grep -q "HISTORY" "$W/card.out" || fail "card: $(cat "$W/card.out")"
"$NW" results --json | "$PY" -c 'import json,sys; j=json.load(sys.stdin); assert all(d["runs"] == 2 for d in j["drives"]), j' \
  || fail "history should hold 2 runs per drive"
cat "$W/card.out"

wait_pct() {  # wait until the running job's drive passes $1 %
  for _ in $(seq 120); do
    EXPR='max([d["progress"].get("pct") or 0 for d in j["drives"]])'
    p=$(js); python3 -c "import sys; sys.exit(0 if float('$p') >= $1 else 1)" && return 0; sleep 0.5
  done; fail "job never reached $1 %"
}
job_id() { EXPR='j["id"]'; js; }

echo "== 2. SIGKILL -> interrupted -> resume"
"$NW" run --drives "$big" --goal check --background > /dev/null
wait_pct 15; J=$(job_id)
sleep 11                                    # past one 10 s checkpoint
systemctl kill -s KILL "nandwash-job-$J"; sleep 1
EXPR='j["effective_state"]'; [ "$(js "$J")" = interrupted ] || fail "after SIGKILL: $(js "$J")"
runok "$NW" resume "$J" > "$W/r1.out" 2>&1 || fail "resume after kill: $(tail -3 "$W/r1.out")"
EXPR='j["state"]'; [ "$(js "$J")" = done ] || fail "resumed job: $(js "$J")"

echo "== 3. SIGTERM -> paused -> resume from the checkpoint"
"$NW" run --drives "$big" --goal check --background > /dev/null
wait_pct 30; J=$(job_id)
systemctl stop "nandwash-job-$J"; sleep 1
EXPR='j["state"]'; [ "$(js "$J")" = paused ] || fail "after SIGTERM: $(js "$J")"
EXPR='j["drives"][0]["progress"]["pct"]'; at=$(js "$J")
"$NW" resume "$J" --background > /dev/null
"$NW" status "$J" > "$W/follow.out" 2>&1 || true            # follows until the job ends
EXPR='j["state"]'; [ "$(js "$J")" = done ] || fail "resumed job: $(js "$J")"
EXPR='j["drives"][0]["results"]["scan"]["report"]'; rep=$(js "$J")
"$PY" - "$rep" <<'EOF' || fail "latency map has gaps after resume"
import sys
from nandwash import report as r
rep, lat = r.load(sys.argv[1])
gaps = sum(1 for x in lat if x == 0)
print(f"   report complete={rep['complete']} chunks={len(lat)} unmeasured={gaps}")
sys.exit(0 if rep["complete"] and gaps == 0 else 1)
EOF
echo "   paused at $at %, resumed to done"

echo "== 4. cancel"
"$NW" run --drives "$big" --goal check --background > /dev/null
wait_pct 5; J=$(job_id)
"$NW" cancel "$J" > /dev/null; sleep 2
EXPR='j["state"]'; [ "$(js "$J")" = cancelled ] || fail "after cancel: $(js "$J")"
"$NW" resume "$J" > /dev/null 2>&1 && fail "a cancelled job resumed"

echo "PASS"
