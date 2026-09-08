#!/usr/bin/env python3
"""Check for a newer Hermes release and verify this fork still fits it.

Bumping ``ARG HERMES_IMAGE`` is the easy half. The hard half is that the
low-memory profile reaches into upstream's internals: it disables toolsets by
name, writes config keys by name, compiles a C stub with the image's gcc,
deletes Playwright's browser directory, and installs s6 hooks that must sort
after upstream's own. None of that is API — upstream can move any of it in a
release without anything looking broken until the container is live on Render
and OOM-killed or missing a listener.

So this does both: report what is available, then check each of those
assumptions against the candidate release's source before letting you take it.

    scripts/check-upstream.py              # what's new + do our assumptions hold
    scripts/check-upstream.py --bump       # ...and rewrite the pin if they do
    scripts/check-upstream.py --tag v2026.8.3   # check a specific release

Exit status is 0 when the pin is current or a clean upgrade is available, 1
when a check failed (read the FAIL lines — each says what in this repo breaks),
2 on a network or usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "NousResearch/hermes-agent"
DOCKER_REPO = "nousresearch/hermes-agent"
DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
ARG_RE = re.compile(
    r"^(ARG HERMES_IMAGE=docker\.io/nousresearch/hermes-agent:)(\S+)$", re.M
)

# Each check names a fact about the upstream image that something in THIS repo
# depends on. `pattern` is searched in `path` at the candidate tag; `breaks`
# says what stops working here if it is gone. Keep this list honest — a check
# that cannot fail is worse than no check, because it buys false confidence.
CHECKS = [
    {
        "name": "gcc in runtime image",
        "path": "Dockerfile",
        # Deliberately just the word: upstream splits `apt-get install` and its
        # package list across lines, so anchoring on `apt-get.*gcc` reports a
        # break that isn't one.
        "pattern": r"\bgcc\b",
        "breaks": "Dockerfile compiles scripts/port-stub.c with gcc; without it "
                  "the build fails and no listener ever binds the port.",
    },
    {
        "name": "Playwright browsers path",
        "path": "Dockerfile",
        "pattern": r"ENV PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/\.playwright",
        "breaks": "Dockerfile does `rm -rf /opt/hermes/.playwright`. If the path "
                  "moved, Chromium survives and can still OOM the 512 MB box.",
    },
    {
        "name": "stage2 tolerates a stripped Playwright",
        "path": "docker/stage2-hook.sh",
        # The guard itself, not the comment above it — comments get reworded.
        "pattern": r'\[ -d "\$PLAYWRIGHT_BROWSERS_PATH" \]',
        "breaks": "Boot may now fail hard instead of skipping the missing "
                  "browser directory. Re-read the AGENT_BROWSER_EXECUTABLE_PATH "
                  "probe before trusting the delete.",
    },
    {
        "name": "cont-init hooks still numbered 01/015/02",
        "path": "Dockerfile",
        "pattern": r"/etc/cont-init\.d/01-hermes-setup",
        "breaks": "Our 02-skills-curate and 03-boot-config rely on lexical "
                  "ordering to run AFTER upstream seeds /opt/data. New "
                  "numbering could put them first, against an unseeded config.",
    },
    {
        "name": "s6-rc service tree at /etc/s6-overlay/s6-rc.d",
        "path": "Dockerfile",
        "pattern": r"s6-overlay/s6-rc\.d",
        "breaks": "The port stub is registered as an s6 longrun under "
                  "/etc/s6-overlay/s6-rc.d/.",
    },
    {
        "name": "s6 user bundle (contents.d) still exists",
        # A 404 on this path is the signal; there is no pattern to match.
        "path": "docker/s6-rc.d/user/contents.d/main-hermes",
        "pattern": r"",
        "breaks": "We enable the port stub by touching "
                  "/etc/s6-overlay/s6-rc.d/user/contents.d/port-stub. Without "
                  "this bundle layout the service is never started.",
    },
    {
        "name": "CMD still reaches main-wrapper.sh",
        "path": "Dockerfile",
        "pattern": r"ENTRYPOINT.*(main-wrapper\.sh|entrypoint-dispatch\.sh)",
        "breaks": "Our CMD [\"gateway\", \"run\"] is passed through the "
                  "entrypoint to main-wrapper.sh. A different entrypoint may "
                  "ignore it and boot the interactive TUI in a restart loop.",
    },
    {
        "name": "dashboard gated on HERMES_DASHBOARD",
        "path": "docker/s6-rc.d/dashboard/run",
        "pattern": r"HERMES_DASHBOARD:-",
        "breaks": "We keep the dashboard off to save ~80-120 MB, and the port "
                  "stub steps aside when it is on. Both read this variable.",
    },
    {
        "name": "agent.disabled_toolsets is a known config key",
        "path": "gateway/run.py",
        # The config-key registry entry, not the .get() call site: gateway/run.py
        # went from ~28k lines to ~5.5k between v2026.8.31 and v2026.9.7 and the
        # call site moved, while this registry tuple stayed put.
        "pattern": r'\("agent",\s*"disabled_toolsets"\)',
        "breaks": "This is the mechanism that keeps the browser toolset out of "
                  "the agent's schema — the single largest OOM risk here.",
    },
    {
        "name": "onboarding.profile_build still gates the first-contact offer",
        "path": "agent/onboarding.py",
        "pattern": r'onboarding\.get\("profile_build"\)',
        "breaks": "Without this switch the agent opens every conversation by "
                  "offering to build a user profile and mentioning /help. On a "
                  "diskless box that fires after every spin-down, not once.",
    },
    {
        "name": "zero-tool schema is a supported state",
        "path": "agent/agent_init.py",
        # The whole persona rests on disabling every toolset. If Hermes stops
        # tolerating an empty tool list, that stops being safe.
        "pattern": r"No tools loaded",
        "breaks": "boot-config.py disables all 24 capability toolsets, leaving "
                  "the model no tools. Hermes handling that gracefully is the "
                  "assumption the whole character rests on.",
    },
    {
        "name": "SOUL.md read from HERMES_HOME",
        "path": "agent/prompt_builder.py",
        # The path is not configurable, so scripts/install-soul.sh writes
        # straight to $HERMES_HOME/SOUL.md. If the identity slot ever moves,
        # the persona silently stops loading — nothing errors.
        "pattern": r'_home / "SOUL\.md"',
        "breaks": "scripts/install-soul.sh installs the persona at "
                  "$HERMES_HOME/SOUL.md. A moved identity slot means the "
                  "agent quietly boots with the stock Hermes persona.",
    },
    {
        "name": "stage2 still reseeds SOUL.md each boot",
        "path": "docker/stage2-hook.sh",
        "pattern": r'seed_one "SOUL\.md"',
        "breaks": "Informational: this reseeding is WHY install-soul.sh has to "
                  "run every boot. If it stops, the hook is harmless but the "
                  "untouched-file check in it needs revisiting.",
        "informational": True,
    },
    {
        "name": "HERMES_ENVIRONMENT_HINT still overrides config",
        "path": "agent/prompt_builder.py",
        "pattern": r'getenv\("HERMES_ENVIRONMENT_HINT"\)',
        "breaks": "render.yaml describes the runtime through this variable so "
                  "the facts stay out of SOUL.md. If it is gone, the agent no "
                  "longer knows it has no browser and no persistent disk.",
    },
    {
        "name": "browser tools still shipped by default",
        "path": "toolsets.py",
        "pattern": r'"browser_navigate"',
        "breaks": "Informational: if browser tools left the default set, "
                  "disabling them matters less. Not a failure on its own.",
        "informational": True,
    },
]

# Toolset names scripts/boot-config.py disables, and the config keys it
# writes. Verified against the candidate's own sources rather than assumed.
DISABLED_TOOLSETS = [
    "browser", "computer_use", "terminal", "code_execution", "delegation",
    "file", "web", "search", "x_search", "skills", "cronjob", "memory",
    "todo", "clarify", "session_search", "context_engine", "project",
    "vision", "video", "video_gen", "image_gen", "tts", "homeassistant",
    "kanban",
]
CONFIG_KEYS = ["max_live_sessions", "max_concurrent_sessions"]
# DEFAULT_CONFIG moved out of config.py into config_defaults.py in v0.20.x, so
# accept either home.
CONFIG_PATHS = ["hermes_cli/config_defaults.py", "hermes_cli/config.py"]


def fail(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "woolflow-check-upstream"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def latest_release() -> str:
    """Newest published release tag. Prefers `gh` (authenticated, no rate
    limit) and falls back to the public API."""
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{REPO}/releases/latest", "--jq", ".tag_name"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    try:
        return http_json(f"https://api.github.com/repos/{REPO}/releases/latest")["tag_name"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
        fail(f"cannot reach the GitHub release API ({exc})")


def image_tag_exists(tag: str) -> bool:
    """A GitHub release exists before its image is pushed; deploying a tag Docker
    Hub does not have yet fails the build several minutes in."""
    try:
        http_json(f"https://hub.docker.com/v2/repositories/{DOCKER_REPO}/tags/{tag}")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        return True  # Hub trouble is not the release's fault; don't block on it
    except urllib.error.URLError:
        return True


def fetch(tag: str, path: str) -> "str | None":
    url = f"https://raw.githubusercontent.com/{REPO}/{tag}/{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "woolflow-check-upstream"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        fail(f"cannot fetch {path}@{tag} ({exc})")
    except urllib.error.URLError as exc:
        fail(f"cannot fetch {path}@{tag} ({exc})")


def current_pin() -> str:
    m = ARG_RE.search(DOCKERFILE.read_text(encoding="utf-8"))
    if not m:
        fail(f"no `ARG HERMES_IMAGE=docker.io/{DOCKER_REPO}:<tag>` line in {DOCKERFILE}")
    return m.group(2)


def run_checks(tag: str) -> bool:
    """Returns True when every non-informational check passed."""
    ok = True
    sources: dict[str, "str | None"] = {}
    for check in CHECKS:
        path = check["path"]
        if path not in sources:
            print(f"  fetching {path} ...", end="\r", file=sys.stderr)
            sources[path] = fetch(tag, path)
        src = sources[path]
        info = check.get("informational", False)

        if src is None:
            status, hit = ("WARN", False) if info else ("FAIL", False)
        else:
            hit = re.search(check["pattern"], src) is not None
            status = "ok" if hit else ("WARN" if info else "FAIL")
        if status == "FAIL":
            ok = False
        print(f"  [{status:>4}] {check['name']}")
        if status != "ok":
            reason = "file is gone" if src is None else "pattern not found"
            print(f"         {reason} in {path}")
            print(f"         {check['breaks']}")

    # Toolset names we disable must still exist, or boot-config.py silently
    # writes entries that match nothing and the browser comes back.
    toolsets_src = sources.get("toolsets.py") or fetch(tag, "toolsets.py") or ""
    missing = [t for t in DISABLED_TOOLSETS if f'"{t}":' not in toolsets_src]
    if missing:
        ok = False
        print(f"  [FAIL] toolset names in WOOLFLOW_DISABLED_TOOLSETS")
        print(f"         gone from toolsets.py: {', '.join(missing)}")
        print("         scripts/boot-config.py would disable nothing for those.")
    else:
        print("  [  ok] toolset names in WOOLFLOW_DISABLED_TOOLSETS")

    # Session caps, wherever DEFAULT_CONFIG lives this release.
    defaults = ""
    for path in CONFIG_PATHS:
        defaults = fetch(tag, path) or ""
        if all(f'"{k}"' in defaults for k in CONFIG_KEYS):
            break
    missing_keys = [k for k in CONFIG_KEYS if f'"{k}"' not in defaults]
    if missing_keys:
        ok = False
        print("  [FAIL] session cap config keys")
        print(f"         not in {' or '.join(CONFIG_PATHS)}: {', '.join(missing_keys)}")
        print("         scripts/boot-config.py would write keys Hermes ignores.")
    else:
        print("  [  ok] session cap config keys")

    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bump", action="store_true",
                    help="rewrite ARG HERMES_IMAGE when every check passes")
    ap.add_argument("--tag", help="check this release instead of the newest")
    ap.add_argument("--force", action="store_true",
                    help="with --bump, write the pin even if checks failed")
    args = ap.parse_args()

    pinned = current_pin()
    target = args.tag or latest_release()

    print(f"pinned:  {pinned}")
    print(f"latest:  {target}" + ("  (same)" if target == pinned else ""))

    # A GitHub release lands before its image is pushed, so a tag can be real
    # and undeployable at the same time. Say so, but still check the source —
    # knowing in advance whether the next release breaks us is the point.
    image_ok = image_tag_exists(target)
    if not image_ok:
        print(f"image:   NOT on Docker Hub yet (pushed a few minutes after the "
              f"release)")

    if target == pinned and not args.tag:
        print("\nAlready current.")
        return 0

    print(f"\nChecking this fork's assumptions against {target}:")
    ok = run_checks(target)
    print()

    if not ok:
        print("Some assumptions no longer hold. Read the FAIL lines above — each")
        print("names what in this repo stops working. Fix those before bumping.")
    elif not image_ok:
        print(f"All checks passed, so {target} looks safe to take — but its image")
        print("is not published yet. Re-run this in a while, then --bump.")
    elif not args.bump:
        print(f"All checks passed. Bump with:\n\n    scripts/check-upstream.py --bump\n")

    if not args.bump:
        return 0 if ok else 1

    if not args.force:
        if not ok:
            print("Pin NOT changed (use --force to override).")
            return 1
        if not image_ok:
            print("Pin NOT changed: the image would fail to pull.")
            return 2

    text = DOCKERFILE.read_text(encoding="utf-8")
    DOCKERFILE.write_text(ARG_RE.sub(rf"\g<1>{target}", text, count=1), encoding="utf-8")
    print(f"Dockerfile pin: {pinned} -> {target}")
    print("Commit, push, and redeploy. Watch the first `[woolflow] mem used=`")
    print("lines afterwards — a new Hermes can change the memory baseline.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
