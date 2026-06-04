#!/usr/bin/env python3
"""touch-grass CLI — backs the /touch-grass slash command.

Usage: cli.py [status|config <key> <value>|snooze|break|override|on|off]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook import (ACTIVE_TERMINAL_SEC, DEFAULT_CONFIG, clear_penalty, config_path,
                  data_dir, fmt_dur, load_config, load_state, log_override,
                  save_state, _atomic_write)

BOOL_KEYS = {"enabled", "macos_notifications"}
INT_KEYS = {"stretch_interval_min", "penalty_after_min", "penalty_break_min",
            "penalty_increment_min", "idle_resets_after_min", "snooze_min"}
STR_KEYS = {"escape_phrase"}


def status():
    cfg = load_config()
    st = load_state()
    now = int(time.time())
    started = max(st["work_started_at"], st["last_break_at"]) or now
    active = max(0, now - started)
    terminals = sum(1 for v in st["sessions"].values()
                    if isinstance(v, dict) and now - v.get("last_seen", 0) <= ACTIVE_TERMINAL_SEC)
    p = st["penalty"]

    print("🌱 touch-grass status")
    print("  enabled:           %s" % cfg["enabled"])
    print("  active work time:  %s" % fmt_dur(active))
    print("  active terminals:  %d" % terminals)
    if p["active"]:
        if now < p["ends_at"]:
            print("  PENALTY ACTIVE:    %s remaining (until %s), %d violation(s)"
                  % (fmt_dur(p["ends_at"] - now),
                     time.strftime("%H:%M", time.localtime(p["ends_at"])), p["violations"]))
        else:
            print("  penalty:           served — cleared on your next prompt")
    else:
        print("  next stretch nudge: in %s" % fmt_dur(
            max(0, cfg["stretch_interval_min"] * 60 - active)))
        print("  penalty box:        in %s" % fmt_dur(
            max(0, cfg["penalty_after_min"] * 60 - active)))
    if now < st.get("snooze_until", 0):
        print("  snoozed:           %s remaining" % fmt_dur(st["snooze_until"] - now))
    print("  config:            %s" % json.dumps(
        {k: cfg[k] for k in sorted(DEFAULT_CONFIG)}, separators=(", ", ": ")))
    print("  data dir:          %s" % data_dir())


def set_config(key, value):
    if key not in DEFAULT_CONFIG:
        print("Unknown config key %r. Valid keys: %s" % (key, ", ".join(sorted(DEFAULT_CONFIG))))
        return 1
    if key in BOOL_KEYS:
        if value.lower() not in ("true", "false"):
            print("%s must be true or false" % key)
            return 1
        parsed = value.lower() == "true"
    elif key in INT_KEYS:
        try:
            parsed = int(value)
        except ValueError:
            parsed = 0
        if parsed <= 0:
            print("%s must be a positive integer (minutes)" % key)
            return 1
    else:
        parsed = value
        if not parsed.strip():
            print("%s must be a non-empty string" % key)
            return 1
    cfg = load_config()
    cfg[key] = parsed
    _atomic_write(config_path(), cfg)
    print("Set %s = %s" % (key, json.dumps(parsed)))
    return 0


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        status()
        return 0
    if cmd == "config":
        if len(argv) != 3:
            print("Usage: config <key> <value>")
            return 1
        return set_config(argv[1], argv[2])
    st = load_state()
    now = int(time.time())
    if cmd == "snooze":
        mins = load_config()["snooze_min"]
        st["snooze_until"] = now + mins * 60
        save_state(st)
        print("Stretch reminders snoozed for %d min." % mins)
        return 0
    if cmd == "break":
        st["last_break_at"] = now
        st["work_started_at"] = now
        save_state(st)
        print("Break logged — work clock reset. Go touch some grass. 🌱")
        return 0
    if cmd == "override":
        if st["penalty"]["active"]:
            clear_penalty(st)
            st["last_break_at"] = now
            st["work_started_at"] = now
            log_override("command", "/touch-grass override")
            save_state(st)
            print("Penalty overridden (logged to overrides.log). Take that break soon.")
        else:
            print("No active penalty to override.")
        return 0
    if cmd in ("on", "off"):
        cfg = load_config()
        cfg["enabled"] = cmd == "on"
        _atomic_write(config_path(), cfg)
        print("touch-grass %s." % ("enabled" if cmd == "on" else "disabled"))
        return 0
    print("Unknown subcommand %r. Usage: status|config <key> <value>|snooze|break|override|on|off" % cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
