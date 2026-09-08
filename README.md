# Hermes Agent on Render, pre-baked with Render tools

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) (the self-improving AI agent from Nous Research) on Render as a single Docker web service, **already wired up to your Render account**. The image extends the upstream Hermes container with:

- The [Render MCP server](https://render.com/docs/mcp-server) registered in `config.yaml` at boot, so MCP tools appear as `mcp_render_list_services`, `mcp_render_get_metrics`, `mcp_render_list_logs`, etc. The agent gets the full MCP tool catalog that your API key can use.
- The official [render-oss/skills](https://github.com/render-oss/skills) bundle (22 Render skills) pinned at a commit and exposed via `skills.external_dirs`.
- A `render-on-hermes` overlay skill that tells the agent the MCP server is already wired up, that the CLI is not installed, and how to behave when an upstream skill expects either.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy-template/api/github/start?template_repo=hermes-render)

The Hermes release and the skills commit are both pinned in the `Dockerfile` for reproducible deploys. All Hermes state lives on a persistent disk so upgrades stay non-destructive, and the dashboard at the service URL is the primary setup surface.

> **Use at your own risk:** The agent can use every Render MCP tool allowed by `RENDER_MCP_API_KEY`, including tools that mutate resources. Lock down dashboard access and use the least-privileged Render account you can.

## Architecture

```
                            ┌──────────────────────────────────────────────┐
                            │ Render web service (Docker, plan: standard)  │
                            │                                              │
   you / external clients   │  ┌────────────────────────────────────────┐  │
   ─────────HTTPS──────────►│  │  hermes dashboard (port 10000)         │  │
                            │  │  - /api/status (healthcheck)            │  │
                            │  │  - browser UI: config / keys / chat    │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │  ┌────────────────────────────────────────┐  │
   Telegram / Discord /  ◄──┤  │  hermes gateway run (foreground)       │  │
   Slack / etc. (outbound)  │  │  - registers Render MCP @ boot         │  │
   Render MCP @ mcp.render  │  │  - calls mcp_render_* tools            │  │
   ◄──────HTTPS────────────►│  │  - long-polls chat platforms           │  │
                            │  │  - spawns subagents per task           │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │                  ▼                           │
                            │  ┌────────────────────────────────────────┐  │
                            │  │  /opt/data (persistent disk, 5 GB)     │  │
                            │  │  .env, config.yaml, sessions/,         │  │
                            │  │  skills/, memories/, logs/             │  │
                            │  └────────────────────────────────────────┘  │
                            │                                              │
                            │  Image-baked, read-only:                     │
                            │   /opt/render-tools/skills-upstream (skills) │
                            │   /opt/render-tools/skills-local    (overlay)│
                            └──────────────────────────────────────────────┘
```

A single container runs both Hermes processes. The dashboard ([upstream docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/web-dashboard.md)) is a side-process that the upstream entrypoint backgrounds whenever `HERMES_DASHBOARD=1` is set; the gateway is the foreground PID. They share `/opt/data` and a PID namespace, which is required for the dashboard's gateway-liveness checks.

The disk holds everything that should survive a redeploy: API keys (`.env`), config (`config.yaml`), the FTS5 session database, installed skills, Honcho user models, agent memories, cron job definitions, and logs. The `render-oss/skills` bundle and the bootstrap that registers the Render MCP server are baked into the image (versioned with each deploy), not the disk.

## What's pre-baked for Render

The `Dockerfile` adds two layers on top of `nousresearch/hermes-agent`:

| Layer | Path in container | Source | Pinned via |
|---|---|---|---|
| Render skill bundle | `/opt/render-tools/skills-upstream/` | [render-oss/skills](https://github.com/render-oss/skills) tarball | `RENDER_SKILLS_REF` ARG (commit SHA) |
| Hermes-on-Render overlay | `/opt/render-tools/skills-local/` | [`./skills/`](./skills) in this repo | This repo's commits |

On every boot, [`scripts/bootstrap.sh`](scripts/bootstrap.sh) runs an idempotent patcher ([`scripts/patch-config.py`](scripts/patch-config.py)) that adds two entries to `/opt/data/config.yaml` if they're missing:

```yaml
mcp_servers:
  render:
    url: https://mcp.render.com/mcp
    headers:
      Authorization: "Bearer ${RENDER_MCP_API_KEY}"

skills:
  external_dirs:
    - /opt/render-tools/skills-local
    - /opt/render-tools/skills-upstream
```

The patcher is **insert-only**: it never overwrites edits you make from the dashboard. The `${RENDER_MCP_API_KEY}` placeholder is resolved lazily at gateway startup, so you can rotate the key from Render's **Environment** tab without rebuilding the image — just restart the service.

> **Why `RENDER_MCP_API_KEY` and not `RENDER_API_KEY`?** The standard name is what the `render` CLI looks for. We deliberately don't ship the CLI in this image (see **Security: agent capabilities**). This is still a normal Render API key with the permissions of the user who created it. The nonstandard env var name avoids accidental CLI auto-auth if you later install the CLI manually. Name your CLI key separately.

## Prerequisites

You need:

- **An LLM provider API key.** [OpenRouter](https://openrouter.ai/keys) is the easiest because it routes to most providers behind a single key. Direct keys for Anthropic, OpenAI, Google, or Hugging Face also work.
- **A Render account.** This fork targets the **Free** plan (512 MB, no persistent disk). Everything in [Memory budget](#memory-budget-render-free-512-mb) exists to make Hermes fit there; upstream's template assumed `standard` (2 GB) and a 5 GB disk.

Optional, depending on which channels you want Hermes to listen on:

- **A Render API key**, if you want the bundled MCP server to inspect or manage Render resources. Generate one at [`dashboard.render.com/u/*/settings#api-keys`](https://dashboard.render.com/u/*/settings#api-keys) and paste it as `RENDER_MCP_API_KEY`. The agent runs without it, but can't see anything on your Render account.
- **Telegram bot token** from [@BotFather](https://t.me/BotFather), plus your Telegram user ID from [@userinfobot](https://t.me/userinfobot).
- **Discord bot token** from [discord.com/developers/applications](https://discord.com/developers/applications) (enable the Message Content Intent).
- **Slack bot + app-level tokens** from [api.slack.com/apps](https://api.slack.com/apps) (Socket Mode requires both `xoxb-...` and `xapp-...`).

> [!WARNING]
> **A Render API key can expose every workspace linked to your account.**
>
> Hermes can use the key through MCP to inspect any workspace the key's owner can access. Some MCP tools can mutate resources today, and more write-capable tools may be added over time. Use a dedicated low-privilege Render user when possible, and do not paste a personal Owner key unless you accept that risk.

You don't need any optional keys to deploy. You can fill them in via the Render Dashboard after the service is up. `RENDER_MCP_API_KEY` is gated behind `sync: false` in the Blueprint, so the **Deploy to Render** flow will prompt for it.

## Memory budget (Render Free, 512 MB)

Free gives the container 512 MB and OOM-kills it the moment it goes over, with
no metrics page to tell you how close you were. Hermes' defaults assume a
machine with headroom, so this fork walks each of them back.

### What runs in the container

| Process | When | Cost |
|---|---|---|
| `hermes gateway run` (Python) | always | the agent loop + Telegram long-poll; the bulk of the budget |
| `woolflow-port-stub` (C) | always | ~1 MB — holds the public port, prints the memory line |
| s6-overlay supervision | always | a few MB across `s6-svscan` and its children |
| `hermes dashboard` (Python) | only while `HERMES_DASHBOARD=1` | a second interpreter, ~80-120 MB — turn it back off after setup |
| Chromium / node / MCP servers | never, by construction | see below |

### What was removed

- **Playwright's Chromium** (`rm -rf /opt/hermes/.playwright`). A headless
  Chromium is 150-400 MB. The `browser_*` tools are part of Hermes'
  `_HERMES_CORE_TOOLS`, so on a stock image any Telegram message could have
  spawned one. Deleting the binaries does not shrink the pulled image — a
  whiteout over a parent layer still ships the bytes — it just makes an
  accidental launch fail with "Chrome not found" instead of an OOM kill.
- **The Render MCP server and the render-oss skill bundle.** Every stdio MCP
  server is a resident subprocess of its own.
- **The python3 port stub.** A CPython interpreter costs ~12-15 MB RSS just to
  exist; [`scripts/port-stub.c`](scripts/port-stub.c) does the same job in
  ~1 MB and reports memory while it is at it.

### What is capped at boot

Free has no persistent disk, so `config.yaml` is reseeded from Hermes' defaults
on every wake-up and the profile has to be re-applied each time.
[`scripts/boot-config.py`](scripts/boot-config.py) runs as an s6 cont-init
hook and rewrites these keys — and only these keys:

| config.yaml key | Hermes default | Here | Env var |
|---|---|---|---|
| `max_concurrent_sessions` | unbounded | `1` | `WOOLFLOW_MAX_CONCURRENT_SESSIONS` |
| `max_live_sessions` | `16` | `2` | `WOOLFLOW_MAX_LIVE_SESSIONS` |
| `agent.disabled_toolsets` | `[]` | all 24 capability toolsets — see below | `WOOLFLOW_DISABLED_TOOLSETS` |

Every one is an env var, so retuning the budget is a **Restart**, not a
rebuild. `WOOLFLOW_LOWMEM=0` skips the hook entirely and gives you stock Hermes
behaviour — the right move if you move this service to a paid plan.

Two allocator settings are baked into the image instead, since they only take
effect at process start: `MALLOC_ARENA_MAX=2` (glibc otherwise gives each
thread its own arena and returns freed pages slowly, inflating RSS well past
the live heap) and `NODE_OPTIONS=--max-old-space-size=192` (node sizes its heap
from *host* memory, not the cgroup, so it would happily grow past 512 MB).

### Reading the numbers

The port stub prints the cgroup's own accounting every
`WOOLFLOW_MEM_INTERVAL` seconds:

```
[woolflow] mem used=213.0MiB limit=512MiB (42%)
```

That is `memory.current` for the **whole container**, not one process — which
is exactly the number Render kills on. It is the only view of memory use this
plan offers, so leave it on until you trust the shape of your workload.

### The agent has no tools at all

The 24 disabled toolsets cover all 53 entries of Hermes' `_HERMES_CORE_TOOLS`,
so the model is handed an empty tool list. That is a state Hermes supports
explicitly — `agent_init.py` prints *"No tools loaded (all tools filtered out
or unavailable)"* rather than failing.

The reason is the persona (a character who cannot explain a shell has no
business having one), but it settles the memory question as a side effect.
`browser` was always the largest risk — Playwright's Chromium is 150-400 MB and
`browser_*` ships in Hermes' DEFAULT schema. `terminal` and `code_execution`
were the last uncapped ones, since they let the agent run anything at all
inside a 512 MB cgroup. With the schema empty, nothing in the container can
allocate on the agent's behalf.

What is left is the gateway process itself plus whatever the conversation
holds. Model context is the remaining variable: a long conversation stays in
memory as Python objects for as long as the session is live, which is what
`max_live_sessions` bounds.

If you want the agent to actually *do* things, drop entries from
`WOOLFLOW_DISABLED_TOOLSETS` — it is an env var, so that is a Restart, not a
rebuild. Re-adding `browser` on this plan will OOM the container.

## Agent identity (SOUL.md)

Hermes reads the agent's persona from `$HERMES_HOME/SOUL.md`, and that path is
not configurable. Free has no persistent disk, so upstream's boot hook reseeds
that file from the image's stock `docker/SOUL.md` on every wake-up — a persona
typed into the dashboard survives exactly until the first spin-down.

So the persona is a file in this repo, [`soul/SOUL.md`](soul/SOUL.md), baked
into the image and reinstalled by the `04-soul` cont-init hook on every boot.
Edit it, commit, redeploy.

The hook does not clobber a persona you edited by hand:

| On-disk SOUL.md | What happens |
|---|---|
| missing | installed |
| identical to Hermes' stock seed | installed |
| identical to what we installed last boot | reinstalled (picks up your edits to the repo file) |
| anything else — you edited it | left alone, with a log line |

`WOOLFLOW_SOUL=0` disables the hook entirely; `WOOLFLOW_SOUL=force` overwrites
even a hand-edited file. Neither needs a rebuild.

Hermes' own `_ensure_default_soul_md()` runs later, at gateway start, but it
only replaces files it recognises as auto-seeded templates — any real persona
is left untouched, so it never fights this hook.

### Stripping the assistant out

A persona file alone leaves you with something half character, half bot,
because Hermes adds its own framing around it. `WOOLFLOW_PERSONA=1` (the
default) turns those off in `config.yaml`:

| Setting | What it was adding |
|---|---|
| `onboarding.profile_build: off` | On first contact: *"introduce yourself, mention /help shows commands"*, plus an offer to build a profile of the user |
| `agent.task_completion_guidance: false` | A "# Finishing the job" brief about deliverables and real tool output |
| `agent.verify_guidance: false` | "Verify your work" nudges |
| `agent.parallel_tool_call_guidance: false` | ~70 tokens on batching tool calls that no longer exist |
| `agent.environment_probe: false` | The host's Python/pip/PEP-668 state — an outright anachronism leak |
| `agent.coding_context: off` | A coding operating brief plus a git/workspace snapshot |

The onboarding one matters more here than upstream intends. Its gate is
"has this install ever had a session?", and the session store lives in
ephemeral `/opt/data` — so on Free it reads as *the user's very first message
ever* after **every** spin-down. Left at its default, the agent re-introduces
itself as a program each time the service wakes up.

Turning it off downgrades that to a plain "briefly introduce yourself and
mention /help" note, which has no config gate of its own. That last step is
handled in `soul/SOUL.md`, which tells the character how to read a bracketed
system note: obey its intent, in his own voice, and never repeat a word of it.

### Facts about the runtime go somewhere else

`HERMES_ENVIRONMENT_HINT` (set in [`render.yaml`](render.yaml)) is upstream's
slot for exactly this: it lets the host describe the environment — no browser,
no persistent state, one conversation at a time — *without editing the identity
slot*. It overrides `agent.environment_hint`, lands in the cached part of the
system prompt, and changes with a Restart rather than a rebuild.

Keeping the two apart matters more than it looks. The shipped persona is a
period roleplay character who must never produce anachronisms, so the hint ends
with an explicit instruction that these operational facts are never to be
spoken in character — the model needs to know it has no browser; the character
must not know what a browser is.

Both files are prose sent with every request. There is no practical size limit
(`context_file_max_chars` defaults to a dynamic cap with a 20K-character floor),
but every line is paid for on every turn, so keep them behavioural: a sentence
that does not change how the agent answers is a sentence to cut.

## Deploy

### Option 1: Deploy button

1. Click the **Deploy to Render** button above.
2. Pick a workspace and a service name.
3. Optionally paste your `RENDER_MCP_API_KEY` when prompted, or leave it blank and add it later from the Environment tab. The agent works without it, just without Render tools.
4. Render reads `render.yaml`, generates a value for `HERMES_GATEWAY_TOKEN`, and creates the service. All other env vars start blank.
5. The first deploy builds the image from the `Dockerfile`. Expect a couple of minutes for the upstream pull (~0.94 GB compressed as of v2026.8.31 — upstream slimmed it from ~2.6 GB over the summer) plus our thin layers, then ~1 minute for the gateway to boot.

### Option 2: Manual Blueprint sync

1. Fork this repo.
2. In the Render Dashboard, go to **Blueprints** → **New Blueprint Instance** and point at your fork.
3. Confirm and apply.

### Protect the URL before configuring

The Hermes dashboard has no built-in authentication. Anyone who knows the service URL can read and write your API keys. Before you visit the dashboard for the first time, choose how you want to protect it:

- Put the service behind an auth gateway that verifies a bearer token, OAuth session, or trusted identity provider.
- Keep the dashboard reachable only through a private network path, such as Tailscale.
- Accept the risk for a demo, use low-privilege keys, and delete the service when you're done.

Read the **Security** section before you paste production API keys.

## Post-deploy setup

Once the service is healthy (the **Events** tab shows "Deploy live"), open the URL Render assigned (it ends in `.onrender.com`). You'll see the Hermes dashboard.

The Blueprint deliberately keeps the env-var surface tiny. All provider keys, tool keys, and chat platform tokens are set from the dashboard, not from `render.yaml`. The dashboard writes everything to `/opt/data/.env`, which lives on the persistent disk and survives redeploys.

Walk through these tabs in order:

1. **API Keys**. Paste a key for at least one LLM provider. Pick one:
   - `OPENROUTER_API_KEY` from [openrouter.ai/keys](https://openrouter.ai/keys) routes to most providers behind a single key
   - `ANTHROPIC_API_KEY` from [console.anthropic.com](https://console.anthropic.com) for Claude models direct
   - `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `HF_TOKEN`, etc. for the others
2. **Config**. Set the `model` field at the top of the list. The upstream image's default is `anthropic/claude-opus-4.6`, which works as soon as you've set `ANTHROPIC_API_KEY`. Otherwise pick a model your provider supports (for example, `anthropic/claude-sonnet-4.6` for Anthropic, or any OpenRouter model ID like `openai/gpt-5.5`).
3. **Status**. Confirm the gateway is running and the model is reachable. The "Connected platforms" list will be empty until you add a chat platform.
4. **API Keys** again, optionally. If you want a chat gateway, add the matching tokens: `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`, etc. Use the **Restart gateway** button on the Status tab so the new tokens are picked up.

If you'd rather set keys from the Render Dashboard's **Environment** tab (handy for CI or secrets-manager workflows), that path also works: Render env vars override `/opt/data/.env` at process start. Pick one path and stick with it to avoid drift. **The two `RENDER_*` variables are the exception** — set them from the Render **Environment** tab (not the Hermes dashboard's API Keys tab), since `config.yaml` reads `${RENDER_MCP_API_KEY}` from the gateway process environment.

### Verify the Render tools are wired up

From the dashboard's **Chat** tab, ask Hermes to verify the tools:

```
What Render services are running in my account?
```

The agent should call `mcp_render_list_services` and respond with the list. If it instead tells you "I don't have access to Render tools" or similar, the gateway didn't see `RENDER_MCP_API_KEY` at startup — set it under **Environment** and click **Restart gateway** on the Status tab.

Before you ask the agent to mutate Render resources, read the **Security: agent capabilities** section below. The agent can use every Render MCP tool allowed by your API key.

### Where the "gateway token" fits

The Blueprint generates a `HERMES_GATEWAY_TOKEN` for you. Today, upstream Hermes doesn't read this variable directly at runtime: it's a placeholder for the OpenAI-compatible API server's bearer key. If you opt into the API server (set `API_SERVER_ENABLED=true` from the dashboard's **API Keys** tab, then paste this token into `API_SERVER_KEY`), external HTTP clients can authenticate against `/v1/chat/completions` using `Authorization: Bearer <that value>`.

## Chatting with the agent

The simplest way to talk to your deployed Hermes is the dashboard's **Chat** tab. The Blueprint sets `HERMES_DASHBOARD_TUI=1`, which makes the upstream dashboard expose the full TUI in the browser over a server-side PTY plus xterm.js. Slash commands, model picker, tool-call cards, streaming, sessions: everything works the same as a local terminal.

If you'd rather stay on the command line, two paths work, both because the in-container `hermes` is the same binary as the local CLI:

- **One-shot prompts via Render Shell or SSH.** The browser shell on Render does not allocate a TTY for `runtime: image` services. The interactive REPL (`hermes` with no args) will print a banner and quit immediately with `Warning: Input is not a terminal (fd=0)`. Use the non-interactive form instead:

  ```bash
  /opt/hermes/.venv/bin/hermes chat -q "summarize today's logs"
  ```

  This runs one turn, prints the result, and exits cleanly. You can chain it with `--resume <session-id>` to continue an existing conversation.

- **Real terminal via the Render CLI.** From your local machine:

  ```bash
  render ssh <service-id>
  /opt/hermes/.venv/bin/hermes
  ```

  `render ssh` allocates a PTY, so the interactive REPL works.

The chat tab in the dashboard is still the cleanest UX. Use the CLI fallbacks when you're scripting or already in a terminal context.

## Cost expectations

Costs assume Render's published prices in May 2026 and don't include data egress, which is unmetered for typical Hermes traffic.

| Component                     | Plan                              | Cost            |
|-------------------------------|-----------------------------------|-----------------|
| Web service (`runtime: docker`) | `free` (512 MB / 0.1 CPU)        | $0              |
| Persistent disk                | none — Free has no disk           | $0              |
| **Subtotal (this fork)**       |                                   | **$0/month**    |

The trade is spelled out in [Memory budget](#memory-budget-render-free-512-mb): no browser automation, one active chat at a time, and state that does not survive a restart. If you want Playwright browsing or parallel subagents back, `starter` (512 MB, $7) buys you no extra memory — go to `standard` (2 GB, $25) and drop the low-memory profile with `WOOLFLOW_LOWMEM=0`.

LLM costs are separate and depend entirely on your provider and usage. OpenRouter and Anthropic both report usage in their respective dashboards; Hermes also surfaces per-model usage on its **Analytics** page.

## Updating Hermes

Hermes ships roughly weekly, and the releases are not small — `gateway/run.py`
went from ~28k lines to ~5.5k between v2026.8.31 and v2026.9.7. The low-memory
profile reaches into upstream internals that no release note promises to keep:
toolset names, config keys, the image's gcc, Playwright's directory, and the
cont-init numbering our hooks sort against. A bump that builds cleanly can
still land a container that OOMs on the first message or never opens a port.

So the update flow checks those assumptions before it changes anything:

```bash
scripts/check-upstream.py            # what's new, and does this fork still fit it
scripts/check-upstream.py --bump     # ...and rewrite the pin if every check passed
scripts/check-upstream.py --tag v2026.9.7   # look at a specific release
```

Each check names the thing in *this repo* that stops working if it fails, so a
FAIL is a to-do, not a mystery. `--bump` refuses to write the pin when a check
failed (`--force` overrides) or when the release's image is not on Docker Hub
yet — GitHub publishes the release a few minutes before the image is pushed,
and a pin to a tag that does not exist fails the build well into the deploy.

Then commit, push and deploy. Render will not auto-deploy (the Blueprint sets
`autoDeployTrigger: off`); trigger it from the Dashboard or the
[Render CLI](https://render.com/docs/cli):

```bash
render deploys create <service-id>
```

**Watch the first `[woolflow] mem used=` lines after any bump.** A new Hermes
can move the memory baseline on its own, and that line is the only place the
change shows up before an OOM does.

Two things the checker cannot see, worth a glance at the release notes:

- **New always-on subsystems.** A feature that starts a thread, a cache or a
  sidecar in the gateway costs memory whether or not you use it.
- **New tools in the default schema.** The check flags `browser_*` because that
  is today's problem; a future release could add something equally heavy under
  a name this repo has never heard of. `WOOLFLOW_DISABLED_TOOLSETS` takes it
  without a rebuild.

## Troubleshooting

### Logs

Render keeps logs in the **Logs** tab of your service. Filter by stream:

- The dashboard side-process prefixes its lines with `[dashboard]`.
- Gateway and agent logs are unprefixed.
- For deeper inspection, log files also live on disk at `/opt/data/logs/` (`agent.log`, `errors.log`, `gateway.log`).

You can tail them from the dashboard's **Logs** tab too, or via SSH (next section).

### Shell access

Render gives you SSH into the container. From the service's overview page, click **Shell** (browser PTY) or copy the SSH command from **Settings**.

```bash
# Inspect the data volume.
ls /opt/data
cat /opt/data/.env

# Run the Hermes CLI directly.
/opt/hermes/.venv/bin/hermes status
/opt/hermes/.venv/bin/hermes config get model.default
```

The container runs as the `hermes` user (UID 10000), not root.

### Service won't start

Check the **Events** tab for the deploy that failed, then the **Logs** tab around that timestamp.

| Symptom                                              | Likely cause                                                                 |
|------------------------------------------------------|------------------------------------------------------------------------------|
| `Refusing to start: binding to 0.0.0.0 requires API_SERVER_KEY` | You set `API_SERVER_ENABLED=true` and `API_SERVER_HOST=0.0.0.0` without an `API_SERVER_KEY`. Set the key or flip back to `127.0.0.1`. |
| Health check fails on `/api/status`                  | `HERMES_DASHBOARD` is unset or the dashboard crashed. Check `[dashboard]` lines for a Python traceback. |
| Container OOM-killed                                 | Read the last `[woolflow] mem used=...` line before the kill. If it was already high at idle, the gateway itself has grown — restart. If it spiked during a turn, look at what the conversation was carrying — with every toolset disabled, nothing but context and the gateway itself allocates here. |
| `Permission denied` on `/opt/data/...`               | The disk was attached after a deploy that ran as a different UID. Restart the service; the entrypoint chowns `/opt/data` on boot when run as root. |
| `Warning: Input is not a terminal (fd=0)` then `Goodbye!` when running `hermes` | Render's browser shell pipes stdin instead of allocating a PTY. Chat from the dashboard's **Chat** tab, or use `hermes chat -q "..."`, or `render ssh <service-id>` from a local terminal. |
| `Goodbye! ⚕` in the deploy logs followed by 502s on the URL | The Dockerfile's `ENTRYPOINT` got bypassed somehow (forked the template and overrode it, or set a `dockerCommand` in `render.yaml` without the full upstream chain). The default `ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/render-tools/bootstrap.sh"]` + `CMD ["gateway", "run"]` must stay intact. |
| `Refusing to run the Hermes gateway as root` | Same root cause as above. Restore the Dockerfile's `ENTRYPOINT`/`CMD` so the upstream `entrypoint.sh` can do its `gosu` drop. |
| Dashboard **Chat** tab shows "Chat unavailable: 1" or hangs / 500s on `/api/pty` | Two upstream bugs combined to break the Chat tab on hosted deploys: (1) [#20500](https://github.com/NousResearch/hermes-agent/issues/20500): `/opt/hermes/ui-tui/` ships root-owned but the dashboard runs as the `hermes` user, so the runtime esbuild rebuild fails with `EACCES`. (2) Separate filename mismatch: `_hermes_ink_bundle_stale()` in `hermes_cli/main.py` looks for `packages/hermes-ink/dist/ink-bundle.js`, but `@hermes/ink`'s build script (`esbuild src/entry-exports.ts --outdir=dist`) only produces `entry-exports.js`. The bundle the staleness check expects is never created, so every `/api/pty` connect runs a 28-second `npm run build` that exceeds Render's WebSocket-upgrade timeout. The Dockerfile chowns the directories AND `touch`es the two expected paths at build time so both checks short-circuit. If you've forked the template and removed those lines, restore them. |
| `mcp_render_*` tools missing from Hermes' tool list | The gateway started without `RENDER_MCP_API_KEY`. Add it under the service's **Environment** tab and click **Restart gateway** from the dashboard's Status tab. |
| Agent says it tried to run `render <something>` and got `command not found` | Working as designed — the Render CLI is not installed in this image (see **Security: agent capabilities**). Most CLI capabilities have an MCP equivalent the agent should use instead; the rest (live log streaming, `render psql`, SSH) the user runs from their own machine. |
| `[render-tools] config patch failed; continuing` in the boot logs | Non-fatal. The agent still runs; you just won't see the Render MCP server until you fix it. Usually means `/opt/data/config.yaml` isn't valid YAML — fix it from the dashboard or wipe it (see "Forcing a clean rebuild"). |
| `tirith security scanner enabled but not available`  | Harmless. Tirith is an optional Rust-based command scanner; without it, Hermes uses pattern matching. Ignore unless you specifically want native scanning. |

### Changing env vars

Set, change, or delete env vars under the service's **Environment** tab. Render restarts the container after a save. Hermes also exposes a `/reload` slash command for in-session reloads if you've already started chatting from the CLI; it's not relevant for the gateway, which restarts cleanly.

### Forcing a clean rebuild

If the Hermes data directory gets into a bad state (corrupt session DB, partial skill install), wipe it:

1. SSH in.
2. `mv /opt/data /opt/data.bak && exit`.
3. Restart the service from the Render Dashboard. The entrypoint recreates the directory tree and reseeds defaults.

Or restore the most recent automatic disk snapshot from the **Disks** page.

## Security

There are two distinct security surfaces in this template, and they compound:

1. **Dashboard auth.** Hermes' web dashboard has no authentication. Anyone who reaches the URL can read your provider keys, change configuration, and chat with the agent.
2. **Agent capabilities.** The agent has access to a Render workspace API key via MCP. Depending on that key's role, it can restart services, change env vars, trigger deploys, and run SQL against Render Postgres.

The two compose into a worst case: an unauthenticated user reaches the dashboard, chats with the agent, and asks it to "delete all services in this workspace." This template registers the full Render MCP tool catalog and **does not install the `render` CLI**. The dashboard lock is on you.

### Agent capabilities

The agent can reach Render through MCP. The boot-time patcher registers `mcp_servers.render` without a `tools.include` filter, so Hermes sees every tool exposed by the Render MCP server. The effective permission boundary is the Render role behind `RENDER_MCP_API_KEY`, across every workspace that key can access.

This is intentionally permissive. It avoids tool visibility surprises, but it means the agent can call write-capable tools when the API key allows them. Even if most MCP usage is read-oriented today, treat the dashboard URL and API key like an admin surface.

#### Why we don't ship the Render CLI

The [`render` CLI](https://render.com/docs/cli) is useful for local operator workflows, but this image does not install it. MCP is the supported in-container Render integration. If you need the CLI, install it deliberately and inspect any installer before running it.

The variable bound in the gateway environment is named `RENDER_MCP_API_KEY` rather than the stock `RENDER_API_KEY` so a manually installed CLI does not auto-authenticate from this var. This does not create a different kind of API key. The Render account role behind the key limits agent capabilities.

This trade-off is worth revisiting once Render adds scoped API keys. A read-only-scoped key for routine inspection and a write-scoped key for deliberate actions would be a better posture.

#### Concrete steps to harden further

- **Scope the API key with a workspace member role.** Create a separate Render workspace member with the minimum role you need and use that user's API key for `RENDER_MCP_API_KEY` instead of an Owner key. The agent inherits whatever role the key grants. This is the closest thing to scoped keys available today.
- **Lock the dashboard.** Put authentication or private-network access in front of the service. Without that, anyone reaching the URL can ask the agent to do anything within whatever caps you've set above.

The bundled `render-on-hermes` overlay skill tells the agent that MCP is already configured and that CLI installation is not an automatic fallback. But **do not rely on agent-side guardrails for safety**. An LLM cannot meaningfully self-restrict. Dashboard access control and a least-privileged API key are the real defenses.

### Dashboard access

Even if the Render API key cannot mutate resources, the dashboard still leaks your LLM provider keys to whoever reaches it. Anyone who can chat with the agent can ask it to do anything the API key allows. Lock the dashboard down before pasting any keys.

Two practical options.

#### Option A: Auth gateway

Expose a small authenticated Web Service in front of Hermes and keep Hermes itself private. The gateway verifies a bearer token, OAuth session, or identity-provider token, then forwards approved traffic to Hermes over Render's private network.

This is the most portable option because it does not depend on static client IPs.

#### Option B: Tailscale

Skip the public internet entirely. Run Tailscale on a sidecar (or use Render's [Tailscale template](https://render.com/docs/deploy-tailscale-derp)) and reach the dashboard only from devices on your Tailnet. This takes more setup, but it avoids IP rotation pain and works from anywhere.

#### Notes

- These options compose. For example, an auth gateway can still sit behind a private network path.
- The OpenAI-compatible API server (`API_SERVER_ENABLED=true`) is separate from the dashboard. It uses a bearer token (`API_SERVER_KEY`), so it's safe to expose with a long random key, but this Blueprint doesn't route it publicly.
- For broader Hermes security guidance see the [upstream security doc](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md).

## What this template does and doesn't do

What it does:

- Pins a specific upstream Hermes image and `render-oss/skills` commit for reproducible deploys.
- Runs the Hermes gateway and dashboard inside one container, the way upstream supports.
- Mounts a persistent disk at the upstream-default `HERMES_HOME` path.
- Bakes the official Render skill bundle into the image, plus a small `render-on-hermes` overlay skill that tells the agent how to behave on this host.
- Idempotently patches `config.yaml` on each boot to register the Render MCP server with the full MCP tool catalog available to your API key, without overwriting your edits.
- Generates a `HERMES_GATEWAY_TOKEN` and marks `RENDER_MCP_API_KEY` as `sync: false` so secrets never sync from the repo.
- Sets a healthcheck that probes the dashboard.

What it deliberately doesn't do:

- **It doesn't install the `render` CLI.** MCP is the supported in-container Render integration. Install the CLI only as a deliberate operator choice.
- It doesn't try to add authentication on top of the dashboard. Use an auth gateway, private network path, or another access-control layer you trust.
- It doesn't enable the OpenAI-compatible API server. Flip `API_SERVER_ENABLED=true` and supply `API_SERVER_KEY` if you need it.
- It doesn't ship a default model. Hermes' upstream default is set in `config.yaml`, which lives on disk and is owner-configurable from the dashboard.
- It doesn't configure browser automation tweaks (`--shm-size`, GPU access). Those need an instance type with more RAM, not extra Render config.
- It doesn't fork or modify the upstream `render-oss/skills` content. The overlay in `skills/render-on-hermes/` is the only Hermes-specific addition; everything else is the canonical Render skill bundle.

## License

This template is MIT licensed (see [`LICENSE`](./LICENSE)). Hermes Agent itself is also MIT licensed; see [the upstream LICENSE](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE).
