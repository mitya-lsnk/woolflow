# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render — Free-tier fork of render-examples/hermes-render.
#
# The whole image is shaped by one constraint: Render's Free web service gets
# 512 MB and is OOM-killed the moment it goes over. Hermes' defaults assume a
# machine with headroom, so this fork walks each of them back.
#
# Key decisions vs the upstream template:
# - Pinned to an exact upstream release (the ARG below — bump it with
#   `scripts/check-upstream.py --bump`, which first verifies that everything
#   this file assumes about the image is still true in the candidate release).
#   That image is s6-overlay based: its ENTRYPOINT is entrypoint-dispatch.sh,
#   which execs `/init main-wrapper.sh "$@"` when it is PID 1, so our CMD flows
#   through unchanged and we DO NOT override ENTRYPOINT (the <=5.7
#   tini/bootstrap.sh chain is gone). We hook into s6's boot via
#   /etc/cont-init.d and register a tiny s6 longrun that holds the port open.
#
#   Note the dispatcher's other branch: when the image is NOT PID 1 (Fly
#   Machines, `docker run --init`, some K8s setups) it skips /init entirely,
#   and with it every supervised service — including our port stub, leaving
#   nothing bound and the healthcheck red. Render's Docker runtime gives the
#   entrypoint PID 1, so this is a caveat for other hosts, not for us.
# - The Render MCP server and the 22-skill render-oss bundle are removed. Each
#   stdio MCP server is a separate resident subprocess; on this plan we can
#   afford none.
# - Playwright's Chromium is deleted and the browser toolset is disabled at
#   boot. A headless Chromium is 150-400 MB — the single largest OOM risk here,
#   and `browser_*` sits in Hermes' DEFAULT tool schema, so any Telegram
#   message could have triggered it.
# - A cont-init hook rewrites config.yaml on every boot with this fork's
#   profile: session caps, an empty tool schema, and Hermes' assistant framing
#   switched off. Free has no persistent disk, so config.yaml is reseeded from
#   Hermes' defaults on every wake-up and the profile has to be re-applied each
#   time — including the first-contact "introduce yourself and mention /help"
#   note, whose gate is an empty session store and so fires after every
#   spin-down.
# - The port stub is a ~1 MB C binary instead of a ~12-15 MB python3
#   http.server, and it doubles as the memory reporter (Free has no metrics).
# - HERMES_DASHBOARD defaults to 0; flip it to 1 in the Render Dashboard for
#   temporary browser-based setup, then back to 0. It is a second Python
#   process — expect ~80-120 MB while it runs.

ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.8.31
FROM ${HERMES_IMAGE}

# Make the stub (and the dashboard, if toggled on) bind the public port.
ENV PORT=10000
ENV HOST=0.0.0.0
ENV HERMES_DASHBOARD_PORT=10000
ENV HERMES_DASHBOARD_HOST=0.0.0.0

# ---- Allocator + runtime memory tuning ----
# glibc gives each thread its own malloc arena (up to 8 * nproc) and returns
# freed pages to the OS only grudgingly, so a threaded Python process shows RSS
# well above its live heap. Capping arenas at 2 trades a little allocator
# contention — irrelevant on a 0.1-CPU Free instance — for tens of MB of RSS.
# Both vars reach the agent through s6's container_environment (see
# main-wrapper.sh's `with-contenv` shebang), the same path HERMES_HOME uses.
ENV MALLOC_ARENA_MAX=2
# Cap V8's old space for any node the agent spawns (execute_code, npx-based
# MCP servers). Node sizes its heap from *host* memory, not the cgroup, so on
# a 512 MB container it would otherwise happily grow past the limit.
ENV NODE_OPTIONS=--max-old-space-size=192

# NOTE: the deprecated chown/ink-bundle workarounds from the <=5.7 template are
# intentionally omitted. v2026.7.x bakes correct permissions at build time via
# --chmod, so chowning ui-tui/node_modules is unnecessary and would touch paths
# that the new image manages differently.

# ---- Remove Playwright's Chromium ----
# Belt and braces with the `browser` toolset being disabled in config.yaml: if
# a future Hermes release moves browser tools outside `disabled_toolsets`'
# reach, the agent fails with "Chrome not found" instead of OOM-killing the
# container. Upstream's stage2-hook explicitly tolerates this — its
# AGENT_BROWSER_EXECUTABLE_PATH probe is skipped when the directory is absent
# ("custom builds that strip Playwright").
#
# This does NOT shrink the pulled image: deleting a parent layer's files only
# adds a whiteout, so the bytes still travel. It buys the runtime guarantee,
# not download time.
RUN rm -rf /opt/hermes/.playwright

# ---- Port stub (s6 longrun) so Render sees an open port ----
# Compiled here rather than shipped as a script: gcc is already in the base
# image, and a build-time failure is a failed deploy instead of a service that
# boots with no listener and fails its healthcheck.
COPY scripts/port-stub.c /tmp/port-stub.c
# Dynamically linked on purpose: glibc is already resident for the Python
# gateway, so sharing those pages costs less container memory than a static
# binary carrying its own copy.
RUN gcc -O2 -s -Wall -Wextra -o /usr/local/bin/woolflow-port-stub /tmp/port-stub.c \
 && rm /tmp/port-stub.c \
 && chmod 0755 /usr/local/bin/woolflow-port-stub

# s6-rc requires a `type` file (value "longrun") for every service, or
# s6-rc-compile fails before /init starts and no port opens.
RUN mkdir -p /etc/s6-overlay/s6-rc.d/port-stub \
 && mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d \
 && printf 'longrun\n' > /etc/s6-overlay/s6-rc.d/port-stub/type
COPY --chown=root:root scripts/port-stub-run /etc/s6-overlay/s6-rc.d/port-stub/run
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/port-stub/run \
 && touch /etc/s6-overlay/s6-rc.d/user/contents.d/port-stub

# ---- Boot hooks (s6 cont-init, lexical order) ----
# Upstream installs 01-hermes-setup (stage2: UID remap, chown, config seed,
# skills sync), 015-supervise-perms and 02-reconcile-profiles. Ours sort after
# all of those, so /opt/data is seeded by the time they run.
#
# 02-skills-curate prunes the synced skills to an allowlist. 03-boot-config
# writes the memory AND persona profiles into the freshly seeded config.yaml. Both are
# idempotent and safe on every wake-up, which matters because Free's
# filesystem is ephemeral and re-seeds each time.
COPY --chown=root:root scripts/skills-curate.sh /etc/cont-init.d/02-skills-curate
COPY --chown=root:root scripts/boot-config.py /opt/woolflow/boot-config.py
RUN chmod 0755 /etc/cont-init.d/02-skills-curate /opt/woolflow/boot-config.py \
 && printf '#!/command/with-contenv sh\nexec s6-setuidgid hermes /opt/woolflow/boot-config.py\n' \
        > /etc/cont-init.d/03-boot-config \
 && chmod 0755 /etc/cont-init.d/03-boot-config

# 04-soul installs the agent's identity. Hermes reads it from
# $HERMES_HOME/SOUL.md — a path that is not configurable — and upstream's
# stage2 reseeds that file from the image's stock docker/SOUL.md whenever it is
# missing, which on a diskless Free instance is every single boot. Baking our
# copy in and re-installing it here is what makes the persona survive a
# spin-down. Runs as root (not s6-setuidgid) because it chowns the result.
COPY --chown=root:root soul/SOUL.md /opt/woolflow/SOUL.md
COPY --chown=root:root scripts/install-soul.sh /etc/cont-init.d/04-soul
RUN chmod 0644 /opt/woolflow/SOUL.md \
 && chmod 0755 /etc/cont-init.d/04-soul

# 05-cron re-creates the scheduled jobs. Same reason as the persona: Hermes
# keeps them in cron/jobs.json under HERMES_HOME, which Free wipes on every
# spin-down, so a job created from the chat lasts until the next sleep. The
# installer calls Hermes' own create_job() rather than writing jobs.json, so
# the stored record is always built by the code that owns the schema.
COPY --chown=root:root cron/jobs.yaml /opt/woolflow/cron-jobs.yaml
COPY --chown=root:root scripts/install-cron.py /opt/woolflow/install-cron.py
RUN chmod 0644 /opt/woolflow/cron-jobs.yaml \
 && chmod 0755 /opt/woolflow/install-cron.py \
 && printf '#!/command/with-contenv sh\nexec s6-setuidgid hermes /opt/woolflow/install-cron.py\n' \
        > /etc/cont-init.d/05-cron \
 && chmod 0755 /etc/cont-init.d/05-cron

# Run the gateway as a long-lived daemon. The upstream ENTRYPOINT is
# "/init main-wrapper.sh"; with no args it launches an interactive TUI
# session, which exits immediately in a container (no stdin/tty) and
# restarts in a loop. Passing "gateway run" makes /init start the
# gateway daemon that connects Telegram/Discord etc. from env vars.
CMD ["gateway", "run"]
