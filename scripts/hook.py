#!/usr/bin/env python3
"""touch-grass hook dispatcher.

Wired to SessionStart, SessionEnd, UserPromptSubmit, PostToolUse, and Stop.
Reads the hook event JSON on stdin, dispatches on hook_event_name, and
enforces break discipline:

  - stretch reminders after `stretch_interval_min` of active work
  - walk suggestions when background work is launched
  - a hard-block penalty box after `penalty_after_min`, with +N min per
    early-resume attempt and an escape phrase for emergencies

Exit codes: 0 = allow (stdout may carry injected context), 2 = block the
prompt (stderr carries the user-facing message). Any internal error fails
open (exit 0) so a touch-grass bug can never block real work.

State and config live in $CLAUDE_PLUGIN_DATA (fallback ~/.claude/touch-grass).
No dependencies beyond stock macOS python3; notifications via osascript.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

DEFAULT_CONFIG = {
    "stretch_interval_min": 60,
    "penalty_after_min": 150,
    "penalty_break_min": 30,
    "penalty_increment_min": 5,
    "idle_resets_after_min": 20,
    "snooze_min": 10,
    "macos_notifications": True,
    "escape_phrase": "I touched grass",
    "enabled": True,
}

DEFAULT_STATE = {
    "work_started_at": 0,
    "last_break_at": 0,
    "last_stretch_reminder_at": 0,
    "last_walk_suggestion_at": 0,
    "snooze_until": 0,
    "penalty": {"active": False, "ends_at": 0, "violations": 0},
    "sessions": {},
}

STALE_SESSION_SEC = 1800   # prune sessions not seen for 30 min
ACTIVE_TERMINAL_SEC = 300  # "active terminal" = heartbeat within 5 min
WALK_DEBOUNCE_SEC = 1800   # at most one walk suggestion per 30 min


def data_dir():
    d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/touch-grass")
    os.makedirs(d, exist_ok=True)
    return d


def _load_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_write(path, obj):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tg-tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def config_path():
    return os.path.join(data_dir(), "config.json")


def state_path():
    return os.path.join(data_dir(), "state.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    on_disk = _load_json(config_path())
    if on_disk is None:
        _atomic_write(config_path(), cfg)
    else:
        cfg.update({k: v for k, v in on_disk.items() if k in DEFAULT_CONFIG})
    return cfg


def load_state():
    st = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
    on_disk = _load_json(state_path())
    if on_disk:
        for k in DEFAULT_STATE:
            if k in on_disk and isinstance(on_disk[k], type(DEFAULT_STATE[k])):
                st[k] = on_disk[k]
        for k in DEFAULT_STATE["penalty"]:
            st["penalty"].setdefault(k, DEFAULT_STATE["penalty"][k])
    return st


def save_state(st):
    _atomic_write(state_path(), st)


def fmt_dur(secs):
    mins = max(0, int(round(secs / 60)))
    if mins >= 60:
        return "%d h %02d min" % (mins // 60, mins % 60)
    return "%d min" % mins


def _osa_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(cfg, title, msg):
    if not cfg.get("macos_notifications") or sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s"' % (_osa_escape(msg), _osa_escape(title))],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def spawn_break_watcher():
    """Detached poller that fires a 'break served' notification when the
    penalty actually ends (extensions included). Exits silently if the
    penalty is cleared some other way."""
    if os.environ.get("TOUCH_GRASS_NO_WATCHER"):
        return
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--break-watcher"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def break_watcher():
    deadline = time.time() + 4 * 3600  # absolute safety stop
    while time.time() < deadline:
        st = load_state()
        p = st["penalty"]
        if not p["active"]:
            return 0
        if time.time() >= p["ends_at"]:
            notify(load_config(), "✅ Touch Grass", "Break served — welcome back.")
            return 0
        time.sleep(30)
    return 0


def log_override(source, prompt):
    try:
        with open(os.path.join(data_dir(), "overrides.log"), "a") as f:
            f.write("%s\t%s\t%s\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%S%z"), source, prompt[:200].replace("\n", " ")))
    except OSError:
        pass


def clear_penalty(st):
    st["penalty"] = {"active": False, "ends_at": 0, "violations": 0}


def handle_prompt(cfg, st, data, now, active, terminals):
    prompt = data.get("prompt") or ""
    low = prompt.lower()
    p = st["penalty"]

    if p["active"]:
        # Escape hatch: phrase anywhere in the prompt, or the override command.
        if cfg["escape_phrase"].lower() in low or (
                low.strip().startswith("/touch-grass") and "override" in low):
            clear_penalty(st)
            st["last_break_at"] = now
            st["work_started_at"] = now
            log_override("escape-phrase" if cfg["escape_phrase"].lower() in low else "command", prompt)
            print("Touch-grass: the user used the emergency escape hatch to skip a mandatory "
                  "break. Briefly acknowledge it (overrides are logged) and encourage them to "
                  "take the break as soon as the emergency is handled. Then proceed normally.")
            save_state(st)
            return 0
        if now >= p["ends_at"]:
            # Break served while away.
            st["last_break_at"] = p["ends_at"]
            st["work_started_at"] = p["ends_at"]
            clear_penalty(st)
            print("Touch-grass: the user just returned from completing their mandatory break. "
                  "Open with one short welcome-back line, then answer normally.")
            save_state(st)
            return 0
        # Early resume: extend and block.
        p["violations"] += 1
        p["ends_at"] += cfg["penalty_increment_min"] * 60
        save_state(st)
        remaining = p["ends_at"] - now
        until = time.strftime("%H:%M", time.localtime(p["ends_at"]))
        sys.stderr.write(
            "🌱 TOUCH GRASS — mandatory break in progress.\n"
            "Early-resume attempt #%d: +%d min added to your break.\n"
            "%s remaining — come back at %s.\n"
            "Genuine emergency? Say \"%s\" to override (it's logged)."
            % (p["violations"], cfg["penalty_increment_min"], fmt_dur(remaining), until,
               cfg["escape_phrase"]))
        notify(cfg, "🌱 Touch Grass", "Nope. +%d min. %s remaining (until %s)."
               % (cfg["penalty_increment_min"], fmt_dur(remaining), until))
        return 2

    if active >= cfg["penalty_after_min"] * 60:
        st["penalty"] = {"active": True, "ends_at": now + cfg["penalty_break_min"] * 60,
                         "violations": 0}
        save_state(st)
        until = time.strftime("%H:%M", time.localtime(st["penalty"]["ends_at"]))
        term_note = " across %d Claude terminals" % terminals if terminals > 1 else ""
        sys.stderr.write(
            "🌱 TOUCH GRASS — mandatory break.\n"
            "You've been working for %s%s. Step away for at least %d minutes — "
            "go outside if you can.\n"
            "Come back at %s. Every early attempt to resume adds +%d min.\n"
            "Genuine emergency? Say \"%s\" to override (it's logged)."
            % (fmt_dur(active), term_note, cfg["penalty_break_min"], until,
               cfg["penalty_increment_min"], cfg["escape_phrase"]))
        notify(cfg, "🌱 Touch Grass", "Mandatory %d-min break after %s of work. Back at %s."
               % (cfg["penalty_break_min"], fmt_dur(active), until))
        spawn_break_watcher()
        return 2

    # Advisory stretch reminder.
    if (active >= cfg["stretch_interval_min"] * 60
            and now > st.get("snooze_until", 0)
            and now - st.get("last_stretch_reminder_at", 0) >= cfg["stretch_interval_min"] * 60):
        st["last_stretch_reminder_at"] = now
        term_note = " (with %d Claude terminals open)" % terminals if terminals > 1 else ""
        print("Touch-grass reminder: the user has been working for %s%s. Begin your response "
              "with one short, friendly line telling them to stand up and stretch before "
              "reading further, then answer their request normally." % (fmt_dur(active), term_note))
        notify(cfg, "🌱 Touch Grass", "You've been at it for %s. Stand up and stretch." % fmt_dur(active))
    save_state(st)
    return 0


def handle_post_tool(cfg, st, data, now, active):
    if st["penalty"]["active"]:
        save_state(st)
        return 0
    resp = data.get("tool_response")
    tin = data.get("tool_input")
    background = (isinstance(resp, dict) and resp.get("status") == "async_launched") or (
        isinstance(tin, dict) and tin.get("run_in_background") is True)
    if background and now - st.get("last_walk_suggestion_at", 0) >= WALK_DEBOUNCE_SEC:
        st["last_walk_suggestion_at"] = now
        extra = ""
        if active >= cfg["stretch_interval_min"] * 60 / 2:
            extra = (" The user has already been working for %s, so they're due anyway — "
                     "make the suggestion firm." % fmt_dur(active))
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext":
                "Touch-grass: you just launched long-running background work. In your next "
                "message, tell the user roughly how long you expect to be busy (estimate from "
                "the task) and suggest they use that window to go outside for a walk instead "
                "of watching the terminal. Keep it to 1-2 sentences." + extra,
        }}))
        notify(cfg, "🚶 Touch Grass", "Claude is busy for a while — good time for a walk.")
    save_state(st)
    return 0


def handle_stop(cfg, st, now, active):
    # Fallback reminder when a turn ends; notification only (Stop stdout
    # isn't injected as context).
    if (active >= cfg["stretch_interval_min"] * 60
            and now > st.get("snooze_until", 0)
            and now - st.get("last_stretch_reminder_at", 0) >= cfg["stretch_interval_min"] * 60):
        st["last_stretch_reminder_at"] = now
        notify(cfg, "🌱 Touch Grass", "You've been at it for %s. Stand up and stretch." % fmt_dur(active))
    save_state(st)
    return 0


def main():
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    event = data.get("hook_event_name", "")
    cfg = load_config()
    if not cfg.get("enabled", True):
        return 0
    st = load_state()
    now = int(time.time())
    sid = data.get("session_id") or "unknown"

    # Prune sessions from crashed/abandoned terminals.
    st["sessions"] = {k: v for k, v in st["sessions"].items()
                      if isinstance(v, dict) and now - v.get("last_seen", 0) <= STALE_SESSION_SEC}

    # First run ever: start the clock now, not at the epoch.
    if not st["last_break_at"]:
        st["last_break_at"] = now
    if not st["work_started_at"]:
        st["work_started_at"] = now

    # Organic-break detection: a long gap across ALL sessions counts as a break.
    heartbeats = [v.get("last_seen", 0) for v in st["sessions"].values()]
    last_hb = max(heartbeats) if heartbeats else 0
    if last_hb and now - last_hb >= cfg["idle_resets_after_min"] * 60:
        st["last_break_at"] = now
        st["work_started_at"] = now
        if st["penalty"]["active"] and now >= st["penalty"]["ends_at"]:
            clear_penalty(st)

    if event == "SessionStart":
        st["sessions"][sid] = {"last_seen": now, "cwd": data.get("cwd", "")}
        save_state(st)
        return 0
    if event == "SessionEnd":
        st["sessions"].pop(sid, None)
        save_state(st)
        return 0

    # Heartbeat for activity events.
    entry = st["sessions"].setdefault(sid, {"cwd": data.get("cwd", "")})
    entry["last_seen"] = now

    # Subagent-originated events: heartbeat only, never remind or block.
    if data.get("agent_id"):
        save_state(st)
        return 0

    active = now - max(st["work_started_at"], st["last_break_at"])
    terminals = sum(1 for v in st["sessions"].values()
                    if now - v.get("last_seen", 0) <= ACTIVE_TERMINAL_SEC)

    if event == "UserPromptSubmit":
        return handle_prompt(cfg, st, data, now, active, terminals)
    if event == "PostToolUse":
        return handle_post_tool(cfg, st, data, now, active)
    if event == "Stop":
        return handle_stop(cfg, st, now, active)
    save_state(st)
    return 0


if __name__ == "__main__":
    if "--break-watcher" in sys.argv:
        sys.exit(break_watcher())
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open: never block real work on our own bugs
