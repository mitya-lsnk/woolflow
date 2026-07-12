#!/bin/sh
# Prune Hermes' synced skills down to a fixed allowlist. Idempotent.
# Runs at boot (s6 cont-init) after stage2-hook seeds /opt/data.
#
# Why: Free's filesystem is ephemeral, so skills re-sync on every wake-up.
# Curating each boot keeps only what the owner actually wants and trims RAM
# and startup churn. Removing a skill dir never affects a running process.
set -eu

ALLOWLIST="computer-use design-md dogfood humanizer nano-pdf node-inspect-debugger popular-web-designs requesting-code-review serving-llms-vllm simplify-code spike test-driven-development youtube-content"

# Cover both candidate skill locations (upstream may use either).
for base in "${HERMES_HOME:-/opt/data}/skills" "${HERMES_HOME:-/opt/data}/.hermes/skills"; do
  [ -d "$base" ] || continue
  for d in "$base"/*; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    case " $ALLOWLIST " in
      *" $name "*) : ;;   # on allowlist -> keep
      *) rm -rf "$d" ;;
    esac
  done
done

echo "[woolflow] skills pruned to allowlist: $ALLOWLIST" >&2
exit 0
