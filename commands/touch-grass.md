---
description: Break tracking — status, config, snooze, voluntary break, penalty override
argument-hint: "[status | config <key> <value> | snooze | break | override | on | off]"
allowed-tools: Bash(python3 *)
---

Run the touch-grass CLI and report its output to the user:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cli.py" $ARGUMENTS
```

- With no arguments (or `status`), show the status output verbatim in a code block.
- For `config`, valid keys are: `stretch_interval_min`, `penalty_after_min`, `penalty_break_min`, `penalty_increment_min`, `idle_resets_after_min`, `snooze_min`, `macos_notifications`, `escape_phrase`, `enabled`. Time values are minutes (positive integers); `macos_notifications`/`enabled` are `true`/`false`.
- If the CLI reports an error (unknown key, bad value), relay it and show correct usage.
- For `override`, gently note that overrides are logged and the break should be taken soon.
- Do not editorialize beyond one short line; the CLI output is the answer.
