#!/opt/hermes/.venv/bin/python
"""Enforce this fork's profile in /opt/data/config.yaml on every boot.

Render's Free plan gives the container 512 MB and no persistent disk, so
config.yaml is reseeded from Hermes' defaults on every wake-up. Those defaults
assume a machine with room to spare: the browser toolset is part of
``_HERMES_CORE_TOOLS`` (so the agent can spawn Playwright's Chromium headless
shell at any time — 150-400 MB, an instant OOM kill here), and the gateway
keeps up to 16 live agent sessions cached in memory.

This runs as an s6 cont-init hook after stage2-hook has seeded the file, and
rewrites just the keys that decide the memory ceiling. Everything else in
config.yaml is left exactly as Hermes wrote it.

Unlike the upstream render-tools patcher this is ENFORCING, not insert-only:
on Free there is no disk for a user edit to persist on, so "don't clobber the
operator's choice" has nothing to protect. Tune it from the Render Dashboard's
Environment tab instead — every value below reads from an env var, so changing
the memory profile is a restart, not a rebuild:

    WOOLFLOW_LOWMEM=0                 skip the memory caps, use Hermes' defaults
    WOOLFLOW_MAX_LIVE_SESSIONS        LRU cap on cached sessions (default 2)
    WOOLFLOW_MAX_CONCURRENT_SESSIONS  hard cap on active chats (default 1)
    WOOLFLOW_DISABLED_TOOLSETS        comma-separated; replaces the default set
    WOOLFLOW_PERSONA=0                keep Hermes' assistant framing (see below)

Never fails the boot: a bad config.yaml or an unwritable path logs a warning
and exits 0, because a gateway running with default memory settings is still
better than a container that won't start.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"

# Toolsets removed from every agent's schema. This fork runs a roleplay
# persona, not an assistant: the character has no tools, so a tool it cannot
# explain is worse than no tool at all. Between them these 24 cover all 53
# entries of Hermes' _HERMES_CORE_TOOLS, which leaves the model with an empty
# tool list — a state Hermes handles explicitly ("No tools loaded (all tools
# filtered out or unavailable)", agent/agent_init.py).
#
# It is also what finally closes the memory question. `browser` was always the
# largest OOM risk (Playwright's Chromium, 150-400 MB, and browser_* ships in
# the DEFAULT schema); `terminal` and `code_execution` were the last uncapped
# ones, since they let the agent run anything inside a 512 MB cgroup.
#
# Note this only removes TOOLS. Inbound Telegram photos still reach the model
# through image_input_mode, which is a separate path from the vision toolset.
DEFAULT_DISABLED_TOOLSETS = [
    "browser", "computer_use", "terminal", "code_execution", "delegation",
    "file", "web", "search", "x_search", "skills", "cronjob", "memory",
    "todo", "clarify", "session_search", "context_engine", "project",
    "vision", "video", "video_gen", "image_gen", "tts", "homeassistant",
    "kanban",
]

# Prompt sections that make Hermes sound like a work assistant. Each is a
# documented config toggle, and each is both off-voice for a character and
# dead weight in a prompt that is paid for on every turn.
PERSONA_CONFIG = {
    # "# Finishing the job / the deliverable is a working artifact backed by
    # real tool output" — pure coding-assistant framing, and meaningless with
    # no tools.
    ("agent", "task_completion_guidance"): False,
    # "verify your work" nudges, same reason.
    ("agent", "verify_guidance"): False,
    # ~70 tokens instructing the model to batch tool calls it no longer has.
    ("agent", "parallel_tool_call_guidance"): False,
    # Surfaces the host's Python/pip/PEP-668 state in the prompt. Beyond being
    # useless here, it is a direct anachronism leak into a period character.
    ("agent", "environment_probe"): False,
    # The coding operating brief + git/workspace snapshot.
    ("agent", "coding_context"): "off",
}

# The first-contact note. Hermes offers to "build a short profile of you" on
# the very first message ever, mentioning /help — which is exactly the
# half-bot, half-character opening this fork is trying to avoid.
#
# Worse here than upstream intends: the gate is `not has_any_sessions()`, and
# the session store lives in ephemeral /opt/data, so on Free it reads as "first
# message ever" after EVERY spin-down. Left alone this fires forever.
#
# "off" downgrades it to a plain intro note; the note itself has no config
# gate, so soul/SOUL.md carries the instruction for handling it in character.
PERSONA_ONBOARDING = {"profile_build": "off"}

DEFAULT_MAX_LIVE_SESSIONS = 2
DEFAULT_MAX_CONCURRENT_SESSIONS = 1


def _warn(msg: str) -> None:
    print(f"[woolflow] {msg}", file=sys.stderr)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _warn(f"{name}={raw!r} is not an integer; using {default}")
        return default


def _disabled_toolsets() -> list[str]:
    raw = os.environ.get("WOOLFLOW_DISABLED_TOOLSETS", "").strip()
    if not raw:
        return list(DEFAULT_DISABLED_TOOLSETS)
    return [item.strip() for item in raw.split(",") if item.strip()]


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _warn(f"cannot read {path} ({exc}); skipping low-memory profile")
        sys.exit(0)
    return data if isinstance(data, dict) else {}


def _enabled(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in {"0", "false", "no"}


def apply_persona(config: dict) -> list[str]:
    """Strip Hermes' assistant framing out of the prompt. Returns what changed.

    Nothing here is a hack: every key is a documented toggle. Together they
    stop the agent introducing itself as a program, offering to build a user
    profile, mentioning /help, and carrying a coding-assistant operating brief
    into a conversation that has no code in it.
    """
    changed: list[str] = []

    for (section, key), value in PERSONA_CONFIG.items():
        target = config.setdefault(section, {})
        if not isinstance(target, dict):
            _warn(f"{section}: is not a mapping; skipping {section}.{key}")
            continue
        if target.get(key) != value:
            target[key] = value
            changed.append(f"{section}.{key}={value}")

    onboarding = config.setdefault("onboarding", {})
    if not isinstance(onboarding, dict):
        _warn("onboarding: is not a mapping; skipping")
        return changed
    for key, value in PERSONA_ONBOARDING.items():
        if onboarding.get(key) != value:
            onboarding[key] = value
            changed.append(f"onboarding.{key}={value}")

    return changed


def apply_home_channel(config: dict) -> list[str]:
    """Seed platforms.telegram.home_channel from WOOLFLOW_TELEGRAM_CHAT_ID.

    A cron job with deliver="telegram" and no explicit chat resolves to the
    platform's home channel, which is normally written by /sethome. That lands
    in config.yaml — which Free wipes on every spin-down, so the jobs would
    lose their destination on the first sleep. Seeding it from an env var keeps
    the chat id out of the repo and out of a file that does not survive anyway.
    """
    chat_id = os.environ.get("WOOLFLOW_TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        return []
    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        _warn("platforms: is not a mapping; skipping home_channel")
        return []
    telegram = platforms.setdefault("telegram", {})
    if not isinstance(telegram, dict):
        _warn("platforms.telegram: is not a mapping; skipping home_channel")
        return []
    home = {"platform": "telegram", "chat_id": chat_id, "name": "home"}
    if telegram.get("home_channel") == home:
        return []
    telegram["home_channel"] = home
    return ["platforms.telegram.home_channel"]


def apply_profile(config: dict) -> list[str]:
    """Write the memory caps into *config*. Returns what changed."""
    changed: list[str] = []

    live = _env_int("WOOLFLOW_MAX_LIVE_SESSIONS", DEFAULT_MAX_LIVE_SESSIONS)
    concurrent = _env_int(
        "WOOLFLOW_MAX_CONCURRENT_SESSIONS", DEFAULT_MAX_CONCURRENT_SESSIONS
    )
    if config.get("max_live_sessions") != live:
        config["max_live_sessions"] = live
        changed.append(f"max_live_sessions={live}")
    if config.get("max_concurrent_sessions") != concurrent:
        config["max_concurrent_sessions"] = concurrent
        changed.append(f"max_concurrent_sessions={concurrent}")

    return changed


def apply_toolsets(config: dict) -> list[str]:
    """Extend agent.disabled_toolsets. Returns what changed."""
    changed: list[str] = []
    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        _warn("agent: is not a mapping; leaving disabled_toolsets alone")
        return changed

    # Union rather than replace: if a future Hermes release ships its own
    # entries here, or an operator adds one by hand, keep them.
    existing = agent.get("disabled_toolsets")
    existing = list(existing) if isinstance(existing, list) else []
    added = [ts for ts in _disabled_toolsets() if ts not in existing]
    if added:
        agent["disabled_toolsets"] = existing + added
        changed.append(f"agent.disabled_toolsets += {','.join(added)}")

    return changed


def save(path: Path, config: dict) -> None:
    text = yaml.safe_dump(
        config, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    tmp = path.with_suffix(path.suffix + ".woolflow.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    lowmem, persona = _enabled("WOOLFLOW_LOWMEM"), _enabled("WOOLFLOW_PERSONA")
    if not lowmem and not persona:
        print("[woolflow] WOOLFLOW_LOWMEM and WOOLFLOW_PERSONA are both off; "
              "leaving config.yaml alone")
        return 0

    config = load(CONFIG_PATH)
    changed: list[str] = []
    # Toolsets belong to both profiles — they are what keeps the browser (and
    # its 150-400 MB) out of the schema AND what keeps the character toolless —
    # so they are applied unless BOTH switches are off.
    changed += apply_toolsets(config)
    changed += apply_home_channel(config)
    if lowmem:
        changed += apply_profile(config)
    if persona:
        changed += apply_persona(config)
    if not changed:
        print(f"[woolflow] {CONFIG_PATH} already matches the woolflow profile")
        return 0

    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save(CONFIG_PATH, config)
    except OSError as exc:
        _warn(f"cannot write {CONFIG_PATH} ({exc}); continuing with defaults")
        return 0

    print(f"[woolflow] profile applied: {'; '.join(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
