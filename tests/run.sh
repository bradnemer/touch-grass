#!/usr/bin/env bash
# touch-grass test suite — covers PRD test cases TC-01 .. TC-15.
# Each test gets a fresh temp CLAUDE_PLUGIN_DATA, seeds state.json with
# crafted timestamps, pipes hook-event JSON to hook.py, and asserts on
# exit code / stdout / stderr / resulting state.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$ROOT/scripts/hook.py"
CLI="$ROOT/scripts/cli.py"
export TOUCH_GRASS_NO_WATCHER=1   # don't spawn detached pollers during tests
PASS=0
FAIL=0

setup() {
  export CLAUDE_PLUGIN_DATA="$(mktemp -d)"
  # notifications off so tests don't shell out to osascript
  cat > "$CLAUDE_PLUGIN_DATA/config.json" <<'EOF'
{"macos_notifications": false}
EOF
}

# seed_state <python-dict-literal merged over defaults>
seed_state() {
  python3 - "$1" <<'EOF'
import json, os, sys, time
now = int(time.time())
st = {
    "work_started_at": now, "last_break_at": now,
    "last_stretch_reminder_at": 0, "last_walk_suggestion_at": 0,
    "snooze_until": 0,
    "penalty": {"active": False, "ends_at": 0, "violations": 0},
    "sessions": {},
}
st.update(eval(sys.argv[1], {"now": now}))
with open(os.environ["CLAUDE_PLUGIN_DATA"] + "/state.json", "w") as f:
    json.dump(st, f)
EOF
}

# run_hook <stdin-json>  -> sets RC, OUT, ERR
run_hook() {
  OUT_F="$CLAUDE_PLUGIN_DATA/out"; ERR_F="$CLAUDE_PLUGIN_DATA/err"
  printf '%s' "$1" | python3 "$HOOK" > "$OUT_F" 2> "$ERR_F"
  RC=$?
  OUT="$(cat "$OUT_F")"; ERR="$(cat "$ERR_F")"
}

# state_q <python-expr over dict st> -> stdout
state_q() {
  python3 -c "
import json, os, time
now = int(time.time())
st = json.load(open(os.environ['CLAUDE_PLUGIN_DATA'] + '/state.json'))
print($1)"
}

check() {  # check <name> <shell-test...>
  local name="$1"; shift
  if "$@"; then PASS=$((PASS+1)); echo "  ok   $name"
  else FAIL=$((FAIL+1)); echo "  FAIL $name  (rc=$RC out=${OUT:0:80} err=${ERR:0:80})"; fi
}

contains() { case "$1" in *"$2"*) return 0;; *) return 1;; esac; }

echo "TC-01 fresh SessionStart registers session and starts clock"
setup
run_hook '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"/tmp/p"}'
check "exit 0" test "$RC" -eq 0
check "session registered" test "$(state_q "'s1' in st['sessions']")" = "True"
check "work clock started" test "$(state_q "st['work_started_at'] > 0")" = "True"

echo "TC-02 stretch reminder after 61 min of work"
setup
seed_state "{'work_started_at': now-3660, 'last_break_at': now-3660, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"keep going"}'
check "exit 0" test "$RC" -eq 0
contains "$OUT" "stand up and stretch" && R=0 || R=1
check "reminder injected" test "$R" -eq 0
check "reminder timestamp set" test "$(state_q "st['last_stretch_reminder_at'] > 0")" = "True"

echo "TC-03 reminder debounced when one fired 10 min ago"
setup
seed_state "{'work_started_at': now-4500, 'last_break_at': now-4500, 'last_stretch_reminder_at': now-600, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"go"}'
check "exit 0" test "$RC" -eq 0
check "no second reminder" test -z "$OUT"

echo "TC-04 snooze suppresses reminder"
setup
seed_state "{'work_started_at': now-4500, 'last_break_at': now-4500, 'snooze_until': now+600, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"go"}'
check "exit 0" test "$RC" -eq 0
check "no reminder while snoozed" test -z "$OUT"

echo "TC-05 background launch triggers walk suggestion"
setup
seed_state "{'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"PostToolUse","session_id":"s1","tool_name":"Bash","tool_input":{"command":"sleep 1","run_in_background":true},"tool_response":{"status":"async_launched"}}'
check "exit 0" test "$RC" -eq 0
contains "$OUT" "additionalContext" && R=0 || R=1
check "context injected" test "$R" -eq 0
contains "$OUT" "walk" && R=0 || R=1
check "suggests a walk" test "$R" -eq 0

echo "TC-06 walk suggestion debounced within 30 min"
setup
seed_state "{'last_walk_suggestion_at': now-600, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"PostToolUse","session_id":"s1","tool_name":"Bash","tool_response":{"status":"async_launched"}}'
check "exit 0" test "$RC" -eq 0
check "no duplicate suggestion" test -z "$OUT"

echo "TC-07 penalty activates at 151 min of work"
setup
seed_state "{'work_started_at': now-9060, 'last_break_at': now-9060, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"one more thing"}'
check "exit 2 (blocked)" test "$RC" -eq 2
contains "$ERR" "30 minutes" && R=0 || R=1
check "mentions 30-min break" test "$R" -eq 0
check "penalty active" test "$(state_q "st['penalty']['active']")" = "True"

echo "TC-08 early resume adds +5 min and increments violations"
setup
seed_state "{'work_started_at': now-9300, 'last_break_at': now-9300, 'penalty': {'active': True, 'ends_at': now+720, 'violations': 0}, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
END_BEFORE="$(state_q "st['penalty']['ends_at']")"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"im back"}'
check "exit 2 (blocked)" test "$RC" -eq 2
check "+300s added" test "$(state_q "st['penalty']['ends_at']")" -eq $((END_BEFORE + 300))
check "violation counted" test "$(state_q "st['penalty']['violations']")" -eq 1

echo "TC-09 escape phrase clears penalty and logs override"
setup
seed_state "{'work_started_at': now-9300, 'last_break_at': now-9300, 'penalty': {'active': True, 'ends_at': now+720, 'violations': 2}, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"ok fine, I touched grass. prod is down"}'
check "exit 0" test "$RC" -eq 0
check "penalty cleared" test "$(state_q "st['penalty']['active']")" = "False"
contains "$OUT" "escape hatch" && R=0 || R=1
check "override context injected" test "$R" -eq 0
check "override logged" test -s "$CLAUDE_PLUGIN_DATA/overrides.log"

echo "TC-10 returning after break served clears penalty"
setup
seed_state "{'work_started_at': now-11100, 'last_break_at': now-11100, 'penalty': {'active': True, 'ends_at': now-300, 'violations': 1}, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"back, refreshed"}'
check "exit 0" test "$RC" -eq 0
check "penalty cleared" test "$(state_q "st['penalty']['active']")" = "False"
contains "$OUT" "welcome" && R=0 || R=1
check "welcome-back context" test "$R" -eq 0
check "work clock reset" test "$(state_q "now - st['last_break_at'] < 600")" = "True"

echo "TC-11 25-min organic gap counts as a break (no penalty)"
setup
seed_state "{'work_started_at': now-9600, 'last_break_at': now-9600, 'sessions': {'s1': {'last_seen': now-1500, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"back from lunch"}'
check "exit 0 (no penalty)" test "$RC" -eq 0
check "no penalty" test "$(state_q "st['penalty']['active']")" = "False"
check "clock reset to now" test "$(state_q "now - st['last_break_at'] <= 5")" = "True"

echo "TC-12 corrupt state.json regenerates and allows prompt"
setup
echo 'not json {{{' > "$CLAUDE_PLUGIN_DATA/state.json"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"hello"}'
check "exit 0" test "$RC" -eq 0
check "state regenerated" test "$(state_q "isinstance(st['sessions'], dict)")" = "True"

echo "TC-13 enabled=false makes all hooks no-op"
setup
echo '{"enabled": false, "macos_notifications": false}' > "$CLAUDE_PLUGIN_DATA/config.json"
seed_state "{'work_started_at': now-99999, 'last_break_at': now-99999, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"go"}'
check "exit 0" test "$RC" -eq 0
check "no output" test -z "$OUT$ERR"
check "no penalty despite huge work time" test "$(state_q "st['penalty']['active']")" = "False"

echo "TC-14 status reports 3 active terminals"
setup
seed_state "{'sessions': {'s1': {'last_seen': now-10, 'cwd': ''}, 's2': {'last_seen': now-20, 'cwd': ''}, 's3': {'last_seen': now-30, 'cwd': ''}}}"
OUT="$(python3 "$CLI" status)"; RC=$?; ERR=""
check "cli exit 0" test "$RC" -eq 0
contains "$OUT" "active terminals:  3" && R=0 || R=1
check "3 terminals reported" test "$R" -eq 0

echo "TC-15 subagent events heartbeat only — never remind or block"
setup
seed_state "{'work_started_at': now-99999, 'last_break_at': now-99999, 'sessions': {'s1': {'last_seen': now-60, 'cwd': ''}}}"
run_hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","agent_id":"sub-1","prompt":"subagent work"}'
check "exit 0 despite thresholds" test "$RC" -eq 0
check "no output" test -z "$OUT$ERR"
check "heartbeat updated" test "$(state_q "now - st['sessions']['s1']['last_seen'] <= 5")" = "True"

echo
echo "passed: $PASS  failed: $FAIL"
test "$FAIL" -eq 0
