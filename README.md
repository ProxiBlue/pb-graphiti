# pb-graphiti

Cross-session, cross-project memory for Claude Code via [Graphiti](https://github.com/getzep/graphiti) + Neo4j, exposed as an MCP server.

Different from Claude Code's built-in per-project `~/.claude/projects/.../memory/`:

| | Built-in auto-memory | pb-graphiti |
|---|---|---|
| Storage | Flat markdown files | Neo4j graph |
| Scope | Single project (per-cwd) | Cross-project + cross-session |
| Scale | ~50 facts before index gets unreadable | Hundreds+ with native search |
| Supersession | Manual (edit/delete) | Native (X was true until time T, then Y) |
| Cross-fact queries | None | Entity-relation walk via Cypher / MCP search |
| Loaded | Auto every session | Queried on-demand via MCP tools |

Use both. Auto-memory for hard rules that MUST load every session. pb-graphiti for domain facts you want to query when relevant.

## What's in the plugin

```
.claude-plugin/        plugin + marketplace manifest
.mcp.json              MCP client config — URL prompted at enable time (default http://localhost:8765/mcp)
skills/graphiti-usage/ SKILL.md — write/query discipline, group_id scope model
commands/              /pb-graphiti:ingest-folder + /pb-graphiti:ingest-slack
hooks/                 SessionStart recall + PreCompact consolidation hooks
scripts/               Python helpers used by the ingest commands and hooks (stdlib only)
infra/                 docker-compose recipe for the host-side Neo4j + Graphiti stack
```

Installing the plugin gives Claude the MCP client config + the usage skill. **You still need to stand up the server yourself** (see Bootstrap).

## Install

Add the marketplace and enable the plugin:

```bash
# In your project root (or globally via ~/.claude/settings.json)
claude /plugin marketplace add proxiblue/pb-graphiti
claude /plugin install pb-graphiti@pb-graphiti
```

Or for local development, clone and reference by path in `~/.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "pb-graphiti": {
      "source": { "source": "directory", "path": "/path/to/pb-graphiti" }
    }
  },
  "enabledPlugins": {
    "pb-graphiti@pb-graphiti": true
  }
}
```

## Graphiti server. 

The plugin's MCP config defaults to `http://localhost:8765/mcp` (overridable at enable time — see [URL: prompted at install time](#url-prompted-at-install-time) below). That endpoint is YOUR responsibility — run the bundled compose stack on your host (or any reachable host):

We build our own docker image from upstream Graphiti source — the image they ship was stale at the time this plugin was made.

```bash
cd infra/

# 1. Clone graphiti as ./upstream/ (compose builds the MCP image from it)
git clone https://github.com/getzep/graphiti upstream

# 2. Fill in API keys
cp .env.example .env
$EDITOR .env   # at minimum: NEO4J_PASSWORD, ANTHROPIC_API_KEY, VOYAGE_API_KEY

# 3. Up
docker compose up -d

# 4. Verify (note: /mcp, no trailing slash — server 307-redirects /mcp/ → /mcp,
# and many MCP HTTP clients do not follow redirects on POST)
curl -sS -X POST http://localhost:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}'
# Expect: event: message  data: {"jsonrpc":"2.0","id":1,"result":{...}}
# Neo4j browser: http://localhost:7474  (login: neo4j / <your NEO4J_PASSWORD>)
```

### Required API keys

- **`ANTHROPIC_API_KEY`** — entity extraction. Default model is `claude-haiku-4-5` (cheap, fast).
- **`VOYAGE_API_KEY`** — embeddings. Voyage's free tier covers typical solo use.
- **`OPENAI_API_KEY`** — leave the dummy value. graphiti-core hardcodes an OpenAI cross-encoder constructor that needs *any* non-empty string at boot, but the reranker path is rarely called when LLM+embedder are non-OpenAI. Replace with a real OpenAI key only if you start seeing rerank-path failures.

Want different providers (Gemini for LLM, OpenAI embeddings, etc.)? Edit `infra/config/config.yaml` — the [Graphiti config schema](https://github.com/getzep/graphiti/blob/main/mcp_server/config/config.yaml) lists every supported provider.

### URL: prompted at install time

When you enable the plugin, Claude Code prompts for `graphiti_url`. Pick the form that matches **where Claude Code runs**, not where the docker stack runs:

| Where Claude Code runs | URL to enter |
|---|---|
| Same host as the docker-compose stack (most consumers) | `http://localhost:8765/mcp` *(default)* |
| Inside a container that shares the docker host (DDEV, devcontainer) | `http://host.docker.internal:8765/mcp` |

Change later via `/plugin config pb-graphiti` or by editing `~/.claude/settings.json` → `pluginConfigs["pb-graphiti@pb-graphiti"].options.graphiti_url`.

**Gotchas that will silently break the connection:**

1. **Trailing slash.** The server 307-redirects `/mcp/` → `/mcp`; most MCP HTTP clients don't follow 307 on POST. Always use `/mcp` (no slash). Plugin defaults handle this correctly.
2. **Wrong host for the runtime.** `host.docker.internal` does not resolve on a bare Linux host; `localhost` inside a container points at the container itself, not the docker host. Pick the URL based on where Claude is actually running.

## Usage — the `group_id` model

There is **one** Neo4j database backing all your projects. Namespacing is per-call, via the `group_id` field on every `add_memory` / `search_nodes` call.

Three tiers:

- **`group_id="initial_ingest"`** — pinned facts always shown at session start, capped at 20 most recent. Curate manually; no auto-pruning. Use for north-star rules you always want loaded.
- **`group_id="fleet"`** — cross-project facts (methodology, vendor rules, organisation policy).
- **`group_id="<project-id>"`** — project-specific quirks. Resolve from `$DDEV_PROJECT` or `basename $(git rev-parse --show-toplevel)`.

From inside a project, ALWAYS query both project + fleet on dynamic recall: `group_ids: ["<project-id>", "fleet"]`. Surfaces fleet rules everywhere without leaking project A's quirks into project B. `initial_ingest` is loaded automatically by the SessionStart hook in addition to dynamic recall.

Full discipline (when to write, when to read, scope confirmation rule, cypher to move mis-scoped nodes) lives in [`skills/graphiti-usage/SKILL.md`](./skills/graphiti-usage/SKILL.md). The skill auto-surfaces to Claude when the graphiti MCP is reachable.

## Continuous memory — automatic recall and consolidation

Two hooks ship with the plugin and turn on the moment you enable it. They make the read/write loop automatic so you stop re-explaining project context every session.

### SessionStart hook — automatic recall

Every time you start a Claude Code session, the plugin queries Graphiti and injects two tiers of context:

1. **Pinned facts** — all episodes in `group_id="initial_ingest"`, capped at 20 most recent. Use this for fleet-wide rules that should be visible every session regardless of cwd.
2. **Dynamic recall** — top 8 entity nodes for `[<project-id>, "fleet"]` against a default semantic query.

Project id is resolved deterministically: `$DDEV_PROJECT` → git toplevel basename → `fleet`-only fallback. The hook is `async`, so a slow or unreachable Graphiti never blocks session start — if it fails for any reason, the session proceeds with no injection.

What Claude sees as `additionalContext`:

```
## Always-loaded (pinned via group_id='initial_ingest')
1 permanent fact(s):
- **Caveman default response style** — All responses default to caveman style... [src: ~/claude-skills-central/rules/caveman.md]

## Graphiti recall (project=acme-store + fleet)
Top 8 relevant facts from prior sessions:
- **acme-store LIVE branch** [Project] (`acme-store`) — LIVE-equivalent branch is `uat`, not `main`...
- **Vendor X blocked** [Vendor] (`fleet`) — Module quality issues; use Vendor Y instead...
- ...
```

**To pin a fact:** call `add_memory(group_id="initial_ingest", name="...", episode_body="...", source_description="...")` via the graphiti MCP from any session. There's no slash-command wrapper yet — write directly.

### PreCompact hook — automatic consolidation

When the conversation is about to be compacted, the plugin runs an agentic hook that reviews the soon-to-be-lost context and writes any worthwhile facts to Graphiti — without prompting. Scope is auto-resolved by the same rules as SessionStart, overridden to `fleet` for cross-project content (methodology, tool-use, vendor verdicts that apply anywhere).

Facts the hook writes: decisions with rationale, project quirks, incident root causes, vendor verdicts, client preferences, runbook steps. Facts it skips: ephemeral session state, in-progress task lists, anything obtainable from `git log` / `git blame`, restatements of CLAUDE.md.

The hook's auto-write bypasses the [graphiti-usage skill's Hard Rule 1](skills/graphiti-usage/SKILL.md) (which requires user confirmation before any add_memory call). The rule still applies to add_memory calls Claude makes mid-conversation; it only carves out for the non-interactive PreCompact hook.

### Disabling the hooks

If you want recall but not consolidation, or vice versa, edit `~/.claude/settings.json`:

```json
{
  "disableAllHooks": false,
  "hooks": {
    "PreCompact": []
  }
}
```

Or disable both entirely with `"disableAllHooks": true` (kills hooks from every plugin, not just this one).

## Bulk ingestion — folders and Slack history

Two slash commands import external content as Graphiti episodes. Both go through the same `add_memory` tool an agent would use ad-hoc; the scripts just batch the calls.

### `/pb-graphiti:ingest-folder <path>`

Walks a directory and ingests `*.md`, `*.markdown`, `*.txt`, `*.rst` (override with `--include`). Markdown is chunked on `##` headings; plain text on paragraph clusters; default target ~1500 words per chunk. Reference time on each episode is the source file's mtime — Graphiti is bi-temporal, so historical docs get correct valid-at metadata. Source description is the file's `file://` URI so recalled facts can be cited back to the exact document.

Use for:
- **Meeting transcripts** (export as markdown first)
- **PRDs / ADRs / specs**
- **Internal wikis, runbooks, postmortems**
- **Module documentation** — pair with GitNexus: GitNexus indexes the code structure, Graphiti indexes the *why* (purpose, design notes, integration intent from each module's README.md). Example:
  ```
  /pb-graphiti:ingest-folder app/code --include 'README*.md,readme.md,*.md' --group-id <project-id>
  ```
  Future sessions then recall "we have module X in project Y that does Z" without re-reading the codebase.

### `/pb-graphiti:ingest-slack <slack-export.zip>`

Takes the `.zip` Slack produces from *Workspace settings → Import/Export Data → Export*. Writes one episode per channel-per-day, `format=message`. Threads inline under their parent. Channel-join/leave/topic noise filtered. Reference time = midnight UTC on the message day.

Slack chats are where the *why* lives — decisions, vendor verdicts, incident chats. Worth ingesting selectively rather than the whole workspace. Filter flags stack:

| Flag | What it drops | Default |
|---|---|---|
| `--channels '#x,#y'` | Anything not from listed channels | (all channels in export) |
| `--since YYYY-MM-DD` | Days before this date | (no time filter) |
| `--include-keywords '<words>'` | Days that don't mention any of these (substring, case-insensitive) | (no filter) |
| `--exclude-keywords '<words>'` | Days that mention any of these | (no filter) |
| `--include-users '<ids or names>'` | Anything not from these users | (all users) |
| `--exclude-users '<ids or names>'` | Messages from these users (e.g. bots) | (none) |
| `--min-words N` | Per-message: under N words ('lgtm', 'thanks', emoji-only) | 3 |
| `--min-day-messages N` | Days with <N surviving messages after per-message filters | 3 |

For client channels that mix project work and chat, `--include-keywords` is the highest-leverage filter — list the project name, key vendors, ticket prefixes, etc., and entire off-topic days vanish from the plan. Dry-run reports a per-filter dropped-day count so you can tune.

**Citations:** when `--workspace-slug <slug>` is set (e.g. `acmeco` for `https://acmeco.slack.com`), every rendered message gets its Slack permalink inline in the episode body, and the episode's `source_description` becomes the channel-archive URL for that date. Recalled facts can then be cited back to the exact thread. Without `--workspace-slug`, source_description falls back to the structured key `slack:<channel>:<YYYY-MM-DD>` — informational only.

### Citation discipline

Every episode written by either ingest command carries a real link in `source_description` (`file://` URI for docs; Slack archive URL for messages). The shipped `graphiti-usage` skill instructs Claude to surface that source whenever it acts on a recalled fact — same discipline as artefact citation in the investigation protocol. See [`skills/graphiti-usage/SKILL.md`](./skills/graphiti-usage/SKILL.md#citation-discipline--surface-the-source) for the recall-side rules.

### Common to both

- **Confirm scope first.** Both commands prompt for fleet vs project before any write.
- **Dry-run first.** Both default to `--dry-run` in the slash command flow; you see the plan (episode count, sample names) before any episode is created.
- **Dedupe via state file.** `.pb-graphiti-ingest.json` in the cwd records what's been ingested. Re-runs from the same cwd skip already-ingested items. Ctrl-C is safe — state is flushed after every successful write. Pass `--reingest` to force a full re-write.
- **Cost reality.** Every episode is one Anthropic Haiku call (entity extraction) + one Voyage embed call. A year of one Slack channel chunked per-day ≈ 365 episodes ≈ ~$0.50 in Haiku. A folder of 50 medium markdown docs chunked per heading ≈ 200-400 episodes ≈ ~$0.50-1.00.

The scripts are stdlib-only Python (no `pip install` required). Run them directly if you prefer:

```bash
python "$(claude plugin path pb-graphiti)/scripts/ingest_folder.py" --help
python "$(claude plugin path pb-graphiti)/scripts/ingest_slack.py"  --help
```

## Costs

Indicative for a solo developer writing ~5 episodes/day across ~10 projects:

| Service | Tier | Approx monthly cost |
|---|---|---|
| Neo4j Community Edition | Self-hosted (this compose) | $0 |
| Anthropic Haiku (entity extraction) | Pay-as-you-go | <$2 |
| Voyage embeddings | Free tier (200M tokens/month) | $0 |

Heavy fleet writers (50+ episodes/day, 50+ projects) will see Haiku creep toward ~$10/month. Embedder usage stays well inside Voyage free tier.

## Companion plugins / related work

- [`pb-gitnexus`](https://github.com/proxiblue/pb-gitnexus) — structural code graph (gitnexus) for Magento / Mage-OS. Pairs well: gitnexus = code structure, pb-graphiti = domain knowledge.
- Built-in Claude Code auto-memory — keep using it for hard rules.

## License

MIT — see [LICENSE](./LICENSE).
