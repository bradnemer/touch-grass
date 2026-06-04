# 🌱 touch-grass

A Claude Code plugin that makes you take breaks. Three escalating levels of intervention:

1. **Stretch reminders** — after 60 minutes of active work, Claude opens its next response by telling you to stand up and stretch (plus a macOS notification).
2. **Walk suggestions** — when Claude launches long-running background work, it tells you roughly how long it'll be busy and suggests you spend that window outside.
3. **The penalty box** — after 150 minutes of sustained work (tracked **across all your open Claude terminals**), your prompts are hard-blocked for a mandatory 30-minute break. Try to sneak back early and every attempt adds **+5 minutes**. Genuine emergency? Say **"I touched grass"** to override — it's logged.

Organic breaks count: any 20+ minute gap in activity across all sessions resets the work clock, so you're never penalized after lunch.

## Inspiration

[5-minute movement breaks can offset the harms of sitting all day](https://www.npr.org/2026/05/31/nx-s1-5835263/5-minute-movement-breaks-sitting-body-electric) — NPR Body Electric

## Install

```
/plugin marketplace add bradnemer/touch-grass
/plugin install touch-grass@touch-grass
```

For local development:

```
claude --plugin-dir /path/to/touch-grass
```

Requires macOS for notifications (`osascript`); everything else works anywhere `python3` exists. No other dependencies.

Optional: `brew install terminal-notifier` — when present, notifications use it instead of `osascript`, so clicking them does nothing (raw `osascript` notifications open Script Editor on click; macOS offers no way around that).

## Usage

```
/touch-grass                     # status: work time, terminals, penalty state, config
/touch-grass snooze              # mute stretch reminders for 10 min
/touch-grass break               # log a voluntary break (resets the work clock)
/touch-grass override            # emergency-clear an active penalty (logged)
/touch-grass off | on            # disable / enable everything
/touch-grass config <key> <val>  # tune thresholds
```

### Config keys (minutes unless noted)

| Key | Default | Meaning |
|---|---|---|
| `stretch_interval_min` | 60 | Active work before a stretch reminder |
| `penalty_after_min` | 150 | Active work before a mandatory break |
| `penalty_break_min` | 30 | Length of the mandatory break |
| `penalty_increment_min` | 5 | Added per early-resume attempt |
| `idle_resets_after_min` | 20 | Activity gap that counts as an organic break |
| `snooze_min` | 10 | Snooze duration |
| `macos_notifications` | true | Native notifications via osascript |
| `escape_phrase` | "I touched grass" | Emergency override phrase |
| `enabled` | true | Master switch |

Config and state live in `~/.claude/touch-grass/` — intentionally a fixed global dir (not `$CLAUDE_PLUGIN_DATA`, which differs between hook processes, the Bash tool, and each install/dev copy) so every Claude terminal shares one work clock. Overrides are appended to `overrides.log` there, so you can audit your own discipline. Override with `TOUCH_GRASS_DATA_DIR` (used by the tests).

## How it works

It's a plugin (not a skill) because every behavior is event-driven — only hooks can fire automatically:

| Hook | Role |
|---|---|
| `SessionStart` / `SessionEnd` | Register/remove this terminal in shared state |
| `UserPromptSubmit` | Heartbeat; penalty gate (exit 2 blocks the prompt); stretch reminders via injected context |
| `PostToolUse` (Bash/Agent/Workflow) | Heartbeat; detects `async_launched` background work → walk suggestion |
| `Stop` | Heartbeat; fallback stretch notification |

All sessions share one state file (atomic tmp+rename writes), so the work clock, penalty, and terminal count are global. Subagent-originated events only heartbeat — they never remind or block. When a penalty starts, a detached watcher process notifies you the moment your break is served (extensions included).

**Fail-open by design:** any internal error exits 0 within the hook timeout — a touch-grass bug can never block real work. The only thing that blocks you is the penalty box, on purpose.

Known cosmetic limitation: there is no native timer hook in Claude Code, so reminders fire on your next interaction rather than at the exact minute mark — which is fine, because reminders only matter when you're at the keyboard.

## Tests

```
tests/run.sh
```

42 assertions across 15 scenarios (PRD TC-01–TC-15): reminders + debounce + snooze, walk suggestions + debounce, penalty activation/extension/escape/serving, organic-gap detection, corrupt-state recovery, kill switch, multi-terminal counting, and subagent immunity. Tests run against a temp data dir with notifications disabled.

See [touch-grass-prd.md](touch-grass-prd.md) for the full spec.

## Initial spec

> Help me spec and build a "touch-grass" plugin or skill for Claude Code (please assess these requirements and recommend whether a plugin or skill would be best).
>
> Requirements: Help the user take regular breaks away from the computer. Sample scenarios: (1) Time-based trigger (e.g., every 60 minutes), notify the user that it's time to stand up and stretch. (2) Long-running background processes: Inform the user that Claude will be busy for a while (e.g., "the next 35 minutes"), so to maintain your productivity and health, you should also go outside and take a walk. (3) Penalty box: After a long session, or in the middle of one, especially with multiple Claude terminals open, inform the user that it's time to take a break of at least 30 minutes; if the user comes back before then and attempts to resume working, add 5 minutes to the break timer.
