#!/command/with-contenv sh
# Install the fork's SOUL.md (soul/SOUL.md, baked to /opt/woolflow/SOUL.md)
# into HERMES_HOME on every boot.
#
# Why a boot hook and not just a file: Hermes reads the agent's identity from
# $HERMES_HOME/SOUL.md — a path that is not configurable — and Free has no
# persistent disk, so upstream's stage2-hook reseeds that file from the image's
# own docker/SOUL.md on every wake-up. Without this hook the persona resets to
# stock Hermes each time the service spins back up.
#
# Runs as cont-init 04, after 01-hermes-setup has done that seeding. The
# gateway's later _ensure_default_soul_md() only overwrites files it recognises
# as auto-seeded templates, so our content survives it untouched.
#
#   WOOLFLOW_SOUL=0       leave SOUL.md alone; use whatever Hermes seeded
#   WOOLFLOW_SOUL=force   overwrite even a hand-edited SOUL.md
set -eu

MODE="${WOOLFLOW_SOUL:-1}"
[ "$MODE" = "0" ] && exit 0

SRC=/opt/woolflow/SOUL.md
DEST="${HERMES_HOME:-/opt/data}/SOUL.md"
UPSTREAM_SEED=/opt/hermes/docker/SOUL.md
# What we installed last boot. On an ephemeral filesystem this never survives;
# on a mounted disk it is how we tell "nobody touched our file" from "the
# operator edited it in the dashboard".
STAMP="${HERMES_HOME:-/opt/data}/.woolflow-soul-installed"

[ -f "$SRC" ] || { echo "[woolflow] $SRC missing; leaving SOUL.md alone" >&2; exit 0; }

if [ -f "$DEST" ] && [ "$MODE" != "force" ]; then
    # Replace only a file nobody has edited: either Hermes' fresh seed, or the
    # copy we put there ourselves on a previous boot.
    if ! cmp -s "$DEST" "$UPSTREAM_SEED" && ! { [ -f "$STAMP" ] && cmp -s "$DEST" "$STAMP"; }; then
        echo "[woolflow] $DEST has local edits; not overwriting (WOOLFLOW_SOUL=force overrides)" >&2
        exit 0
    fi
fi

cp "$SRC" "$DEST"
cp "$SRC" "$STAMP"
# Match the 0600 + hermes ownership that Hermes' own seeder applies. chown by
# NAME so a HERMES_UID remap at boot is picked up automatically.
chown hermes:hermes "$DEST" "$STAMP" 2>/dev/null || true
chmod 0600 "$DEST" "$STAMP" 2>/dev/null || true
echo "[woolflow] SOUL.md installed from the image ($(wc -c < "$SRC" | tr -d '[:space:]') bytes)"
