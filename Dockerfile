# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render — Free-tier fork of render-examples/hermes-render.
#
# Key decisions vs the upstream template:
# - Pinned to v2026.7.7.2, an s6-overlay image where /init is PID 1. We DO NOT
#   override ENTRYPOINT/CMD (the <=5.7 tini/bootstrap.sh chain is gone). Instead
#   we hook into s6's boot via /etc/cont-init.d (skills curator) and register a
#   tiny s6 longrun that holds the public port open.
# - The Render MCP server and the 22-skill render-oss bundle are removed to keep
#   the image small and RAM low on the 512 MB Free instance.
# - A skills curator prunes the synced skills down to a fixed allowlist each boot.
# - A ~2 MB python http.server stub binds 0.0.0.0:$PORT so Render's healthcheck
#   stays green and the Free service is not flagged "no open ports". Hermes
#   itself talks to Telegram over an outbound long-poll, so it needs no listener.
# - HERMES_DASHBOARD defaults to 0; flip it to 1 in the Render Dashboard for
#   temporary browser-based setup, then back to 0.

ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.7.7.2
FROM ${HERMES_IMAGE}

# Make the stub (and the dashboard, if toggled on) bind the public port.
ENV PORT=10000
ENV HOST=0.0.0.0
ENV HERMES_DASHBOARD_PORT=10000
ENV HERMES_DASHBOARD_HOST=0.0.0.0

# NOTE: the deprecated chown/ink-bundle workarounds from the <=5.7 template are
# intentionally omitted. v2026.7.x bakes correct permissions at build time via
# --chmod, so chowning ui-tui/node_modules is unnecessary and would touch paths
# that the new image manages differently.

# ---- Skills curator: prune synced skills to an allowlist (s6 cont-init) ----
# Runs once at boot, after stage2-hook has seeded /opt/data and run skills-sync.
# Idempotent; safe to run on every wake-up (Free's filesystem is ephemeral).
COPY --chown=root:root scripts/skills-curate.sh /etc/cont-init.d/02-skills-curate
RUN chmod 0755 /etc/cont-init.d/02-skills-curate

# ---- Lightweight port stub (s6 longrun) so Render sees an open port ----
# Hooks into the existing s6 supervision tree; uses only the bundled python3.
RUN mkdir -p /etc/s6-overlay/s6-rc.d/port-stub \
 && mkdir -p /etc/s6-overlay/s6-rc.d/user/contents.d
COPY --chown=root:root scripts/port-stub-run /etc/s6-overlay/s6-rc.d/port-stub/run
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/port-stub/run \
 && touch /etc/s6-overlay/s6-rc.d/user/contents.d/port-stub
