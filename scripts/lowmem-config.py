#!/opt/hermes/.venv/bin/python
"""Enforce the low-memory profile in /opt/data/config.yaml on every boot.

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

    WOOLFLOW_LOWMEM=0                 skip entirely, use Hermes' defaults
    WOOLFLOW_MAX_LIVE_SESSIONS        LRU cap on cached sessions (default 2)
    WOOLFLOW_MAX_CONCURRENT_SESSIONS  hard cap on active chats (default 1)
    WOOLFLOW_DISABLED_TOOLSETS        comma-separated; replaces the default set

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

# Toolsets removed from every agent's schema. Each is either impossible in this
# container or costs more RAM than the instance has:
#   browser       Playwright Chromium headless shell, 150-400 MB per launch.
#                 The single largest OOM risk on this plan.
#   computer_use  macOS-only cua-driver; can never resolve in a Linux container.
#   video         ffmpeg decode of whole video files into memory.
#   video_gen     same, plus multi-hundred-MB downloads.
#   image_gen     decodes generated images in-process before delivery.
#   tts           audio synthesis buffers; lazy-installs edge-tts on first use.
# Disabling a toolset also drops its tools from the JSON schema sent on every
# request, so this trims context tokens as well as resident memory.
DEFAULT_DISABLED_TOOLSETS = [
    "browser",
    "computer_use",
    "video",
    "video_gen",
    "image_gen",
    "tts",
]

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


def apply_profile(config: dict) -> list[str]:
    """Write the low-memory keys into *config*. Returns what changed."""
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
    if os.environ.get("WOOLFLOW_LOWMEM", "1").strip() in {"0", "false", "no"}:
        print("[woolflow] WOOLFLOW_LOWMEM is off; leaving config.yaml alone")
        return 0

    config = load(CONFIG_PATH)
    changed = apply_profile(config)
    if not changed:
        print(f"[woolflow] {CONFIG_PATH} already matches the low-memory profile")
        return 0

    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save(CONFIG_PATH, config)
    except OSError as exc:
        _warn(f"cannot write {CONFIG_PATH} ({exc}); continuing with defaults")
        return 0

    print(f"[woolflow] low-memory profile applied: {'; '.join(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
