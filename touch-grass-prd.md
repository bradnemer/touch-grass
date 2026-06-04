# Touch Grass — Break-Enforcement Plugin for Claude Code

## Overview

**touch-grass** is a Claude Code **plugin** (not a standalone skill) that helps developers take regular breaks away from the computer. It nudges the user to stretch on a schedule, suggests walks when Claude launches long-running background work, and — after sustained work sessions — enforces a mandatory break with an escalating timer that hard-blocks prompts until the break is served.

It is a plugin because all three behaviors are *automated*: they must fire on harness events (prompt submit, tool use, session lifecycle), which only **hooks** can do. Skills are model-invoked and cannot run on timers or intercept prompts. The plugin bundles the hooks, their shared state scripts, and a `/touch-grass` control command into one installable unit distributed via GitHub.

**Target user:** Brad (and any developer who installs it) — runs multiple concurrent Claude Code terminals on macOS, works long sessions, wants externally-enforced break discipline.

## Goals

- Remind the user to stand up and stretch at a configurable interval (default: every 60 minutes of active work).
- When Claude kicks off long-running background work, tell the user how long Claude expects to be busy and suggest going outside.
- After sustained cumulative work **across all open Claude sessions**, enforce a mandatory break (default 30 min). Early-resume attempts are blocked and add +5 minutes each.
- Deliver reminders both **in-conversation** and as **macOS native notifications**.
- All thresholds configurable via a JSON config file and the `/touch-grass` command.
- Global scope: one shared state across every project and terminal.

**Non-goals (v1):**
- No verification that the user actually went outside.
- No Windows/Linux notification support (hooks degrade gracefully; in-conversation messages still work).
- No calendar/health-app integration.
- No per-project configuration overrides.

## User Stories

> As a developer deep in flow, I want a reminder every hour to stand and stretch, so I don't sit motionless for an entire afternoon.

> As a developer whose agent just launched a 35-minute background job, I want Claude to tell me it'll be busy for a while and suggest a walk, so the dead time becomes recovery time.

> As a developer with four Claude terminals open for three hours straight, I want the system to force a 30-minute break — and when I sneak back at minute 12, I want my prompt rejected and the timer extended, so the enforcement has actual teeth.

> As a developer with a genuine emergency (prod incident), I want an escape hatch to end the penalty early, so enforcement never costs me real money.

> As the plugin owner, I want to check my break status and tune thresholds with `/touch-grass`, so I can adapt it without editing scripts.

## Architecture Decision: Plugin vs. Skill

| Requirement | Skill can do it? | Hook (plugin) can do it? |
|---|---|---|
| Fire every ~60 min regardless of conversation topic | ❌ model-invoked only | ✅ check elapsed time on `UserPromptSubmit` / `Stop` |
| Detect background task launches | ❌ | ✅ `PostToolUse` (matcher: `Bash`/`Agent`/`Workflow`, detect `status: "async_launched"`) |
| Block prompts during penalty | ❌ | ✅ `UserPromptSubmit` exit code 2 |
| Track time across multiple terminals | ❌ | ✅ shared state file keyed by `session_id` (provided in hook stdin JSON) |
| User-facing status/config UX | ✅ | command file in plugin |

**Verdict: plugin**, with hooks at the core and a `commands/touch-grass.md` slash command for control. No SKILL.md is needed in v1 — there is no behavior Claude needs to be taught; everything is event-driven.

## Repository Layout

```
touch-grass/                          # github.com/bradnemer/touch-grass
├── .claude-plugin/
│   ├── plugin.json                   # manifest (name, description, version, author)
│   └── marketplace.json              # single-plugin marketplace for GitHub install
├── hooks/
│   └── hooks.json                    # event → script wiring
├── scripts/
│   ├── lib.sh                        # shared: state read/write, config, notify, time math
│   ├── session-start.sh              # SessionStart: register session heartbeat
│   ├── session-end.sh                # SessionEnd: deregister session
│   ├── prompt-gate.sh                # UserPromptSubmit: penalty gate + stretch reminder + heartbeat
│   ├── activity.sh                   # PostToolUse(Bash|Agent|Workflow): heartbeat + background-task detection
│   └── stop-check.sh                 # Stop: heartbeat + stretch reminder fallback
├── commands/
│   └── touch-grass.md                # /touch-grass status|config|snooze|break|override
├── touch-grass-prd.md
└── README.md                         # install instructions, config reference
```

**Install path for users:**
```
/plugin marketplace add bradnemer/touch-grass
/plugin install touch-grass@touch-grass
```
Development: `claude --plugin-dir ~/code/touch-grass`.

## State & Config

All persistent data lives in `${CLAUDE_PLUGIN_DATA}` (falls back to `~/.claude/touch-grass/` when unset):

**`config.json`** (created with defaults on first run):
```json
{
  "stretch_interval_min": 60,
  "penalty_after_min": 150,
  "penalty_break_min": 30,
  "penalty_increment_min": 5,
  "idle_resets_after_min": 20,
  "snooze_min": 10,
  "macos_notifications": true,
  "escape_phrase": "I touched grass",
  "enabled": true
}
```

**`state.json`** (single shared file, all sessions):
```json
{
  "work_started_at": 1750000000,
  "last_break_at": 1750000000,
  "last_stretch_reminder_at": 1750000000,
  "snooze_until": 0,
  "penalty": { "active": false, "ends_at": 0, "violations": 0 },
  "sessions": { "<session_id>": { "last_seen": 1750003600, "cwd": "/Users/brad/code/foo" } }
}
```

- **Active work time** = `now - max(work_started_at, last_break_at)`, where any gap ≥ `idle_resets_after_min` across **all** sessions' heartbeats resets the clock (an organic break counts as a break).
- **Writes are atomic**: write to temp file in same dir, then `mv`. Concurrent hook races are tolerated (stakes are low; last-writer-wins on a heartbeat is harmless). No `flock` (not on stock macOS).
- **Stale sessions**: any session whose `last_seen` is older than 30 min is pruned on every state write (handles crashed terminals).
- All timestamps are Unix epoch seconds; all parsing/JSON via `python3` (ships with macOS dev tools) — **no jq/node dependency**.

## Functional Requirements

### FR-1: Activity tracking & multi-session awareness
1. On `SessionStart`, the hook shall record `{session_id, last_seen, cwd}` in `state.json.sessions` and initialize `work_started_at` if no sessions were previously active.
2. On every `UserPromptSubmit`, `PostToolUse`, and `Stop` event, the hook shall update the session's `last_seen` heartbeat.
3. On `SessionEnd`, the hook shall remove the session entry.
4. If the most recent heartbeat across all sessions is older than `idle_resets_after_min` when a new event arrives, the hook shall treat the gap as a completed break: set `last_break_at` to the gap start, reset `penalty` if its `ends_at` has passed.
5. The count of sessions with `last_seen` within 5 minutes is the **active terminal count**, included in penalty/stretch messaging (e.g., "you have 3 Claude terminals open").

### FR-2: Stretch reminders (scenario 1)
1. On `UserPromptSubmit` (and `Stop` as fallback), if active work time ≥ `stretch_interval_min` AND `now > snooze_until` AND no reminder fired within the last `stretch_interval_min`, the hook shall:
   a. Inject context (plain stdout on `UserPromptSubmit`) instructing Claude to open its response with a short, friendly stand-up-and-stretch reminder, including how long the user has been working.
   b. Fire a macOS notification: `🌱 Touch Grass — You've been at it for {N} min. Stand up and stretch.` (via `osascript -e 'display notification ...'`), if `macos_notifications` is true.
   c. Record `last_stretch_reminder_at`.
2. Stretch reminders shall never block the prompt — they are advisory only.
3. `/touch-grass snooze` shall suppress stretch reminders for `snooze_min` minutes.

### FR-3: Long-running background work (scenario 2)
1. On `PostToolUse` with matcher `Bash|Agent|Workflow`, if `tool_response.status == "async_launched"` (or the tool input had `run_in_background: true`), the hook shall inject `additionalContext` instructing Claude to: estimate how long the background work will take, tell the user (e.g., "I'll be busy for roughly the next 35 minutes"), and suggest using that window to go outside for a walk.
2. If the user's active work time also exceeds half the stretch interval, the injected instruction shall strengthen the suggestion ("you're due anyway").
3. The hook shall also fire a macOS notification: `🚶 Claude is busy — good time for a walk.`
4. At most one walk suggestion per 30 minutes (debounced via state), so parallel agent fan-outs don't spam.

### FR-4: Penalty box (scenario 3)
1. When active work time ≥ `penalty_after_min`, the next `UserPromptSubmit` hook shall activate the penalty: set `penalty.active = true`, `ends_at = now + penalty_break_min * 60`, `violations = 0`, **block the prompt** (exit code 2) with a stderr message stating the mandatory break length, when they may return (local wall-clock time), and the active terminal count.
2. While `penalty.active` and `now < ends_at`, every subsequent `UserPromptSubmit` in **any** session shall:
   a. Increment `violations` and add `penalty_increment_min * 60` to `ends_at`.
   b. Block the prompt with a message showing remaining time, the +5 penalty just applied, and the escape-hatch instructions.
3. Penalty activation and each block shall fire a macOS notification with the remaining time.
4. When the penalty activates, the hook shall schedule a detached one-shot notifier — `nohup sh -c 'sleep {remaining}; osascript … "✅ Break served — welcome back"' >/dev/null 2>&1 &` — re-scheduled (old one's message harmlessly fires early but state says penalty is over… see Edge Cases) whenever `ends_at` changes.
5. When a prompt arrives after `ends_at`, the hook shall clear the penalty, set `last_break_at = ends_at`, reset the work clock, allow the prompt, and inject a brief welcome-back context line.
6. **Escape hatch:** if the prompt text contains the configured `escape_phrase` (case-insensitive) — or the prompt is the `/touch-grass override` command — the hook shall clear the penalty, allow the prompt, and inject context noting the override was used (Claude gently acknowledges it should be for emergencies).
7. Penalty state is global: blocking applies to all sessions/terminals simultaneously.

### FR-5: `/touch-grass` command
A `commands/touch-grass.md` slash command whose instructions tell Claude to run `${CLAUDE_PLUGIN_ROOT}/scripts/lib.sh` subcommands and report results:
1. `/touch-grass` or `/touch-grass status` — show: active work time, time until next stretch reminder, time until penalty threshold, penalty state (remaining + violations), active terminal count, current config.
2. `/touch-grass config <key> <value>` — validate and update `config.json` (numeric keys must be positive integers; `enabled`/`macos_notifications` boolean).
3. `/touch-grass snooze` — suppress stretch reminders for `snooze_min`.
4. `/touch-grass break` — voluntarily start a break now: resets the work clock as if a break was taken (sets `last_break_at = now`); no blocking.
5. `/touch-grass override` — clear an active penalty (the escape hatch in command form).
6. `/touch-grass off` / `on` — toggle `enabled`. When disabled, all hooks no-op immediately (first check in every script).

### FR-6: Resilience
1. Every hook script shall exit 0 (allow) within 2 seconds on any internal error — a bug in touch-grass must never block real work except via the explicit penalty path.
2. Missing/corrupt `state.json` or `config.json` shall be regenerated with defaults.
3. If `osascript` fails or `macos_notifications` is false, skip notifications silently.

## Edge Cases & Error Handling

| Scenario | Expected Behavior |
|---|---|
| User leaves mid-penalty and returns after `ends_at` | First prompt clears penalty, counts break as served, allowed through. |
| Corrupt `state.json` | Regenerate defaults; current prompt allowed. |
| Two hooks write state simultaneously | Atomic tmp+`mv`; last-writer-wins; worst case a heartbeat is lost — acceptable. |
| Crashed terminal leaves stale session entry | Pruned when `last_seen` > 30 min old. |
| Escape phrase used while no penalty active | No-op; prompt allowed normally. |
| Penalty extended after one-shot "break served" notifier scheduled | Stale notifier may fire early; prompt gate is source of truth and still blocks. Acceptable cosmetic flaw, documented in README. |
| System sleep/clock jump during work | Gap ≥ `idle_resets_after_min` reads as a break — correct behavior for sleep. |
| `enabled: false` | Every script exits 0 immediately. |
| Background task detected during active penalty | No walk suggestion (user shouldn't be prompting anyway — they got blocked). |
| Subagent-originated hook events (`agent_id` present in stdin) | Heartbeat only; no reminders/blocks (avoid spamming from agent fan-outs). |

## Test Cases & Acceptance Criteria

State-manipulation harness: tests rewrite timestamps in `state.json`, invoke scripts with crafted stdin JSON, and assert on exit code / stdout / state diffs. Pure bash test script (`tests/run.sh`), no framework.

- [ ] **TC-01** Fresh session start → `sessions` contains the session_id; `work_started_at` set.
- [ ] **TC-02** `work_started_at` 61 min ago, prompt submitted → exit 0, stdout contains stretch-reminder instruction, `last_stretch_reminder_at` updated.
- [ ] **TC-03** Reminder fired 10 min ago, prompt submitted → no second reminder (debounce).
- [ ] **TC-04** Snooze active → no reminder despite threshold exceeded.
- [ ] **TC-05** `PostToolUse` stdin with `tool_response.status: "async_launched"` → stdout JSON contains `additionalContext` with walk suggestion.
- [ ] **TC-06** Same within 30-min debounce window → no suggestion.
- [ ] **TC-07** Active work 151 min, prompt submitted → exit 2, stderr mentions 30-min break, `penalty.active == true`.
- [ ] **TC-08** Penalty active, 12 min remaining, prompt submitted → exit 2, `ends_at` grew by 300 s, `violations` incremented.
- [ ] **TC-09** Penalty active, prompt contains "I touched grass" → exit 0, penalty cleared, stdout notes override.
- [ ] **TC-10** Penalty `ends_at` 5 min in the past, prompt submitted → exit 0, penalty cleared, work clock reset, welcome-back context injected.
- [ ] **TC-11** All heartbeats 25 min old, prompt submitted → gap counted as break; work clock reset; no penalty.
- [ ] **TC-12** `state.json` contains garbage → regenerated; prompt allowed (exit 0).
- [ ] **TC-13** `enabled: false` → all scripts exit 0 with no output, state untouched.
- [ ] **TC-14** Three sessions with fresh heartbeats → status output reports 3 active terminals.
- [ ] **TC-15** Hook stdin includes `agent_id` → heartbeat updated, no reminder/block even past thresholds.

## Out of Scope (v1)

- Linux/Windows notification backends.
- A `monitors/` background poller (could later push reminders while fully idle — v2 candidate).
- Tracking non-Claude computer activity (the user typing in an editor is invisible to us).
- Pomodoro modes, statistics dashboards, streak gamification.
- Per-project config overrides.

## Open Questions

1. ~~Plugin vs skill~~ → **Plugin** (resolved above).
2. Default `penalty_after_min = 150` (2.5 h of continuous work before mandatory break) — confirm or tune; it's config-anyway.
3. Should `/touch-grass override` log overrides to a file so the user can audit their own discipline? (Trivial to add; suggest yes.)
