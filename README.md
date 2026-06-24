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
commands/              /pb-graphiti:ingest-folder + ingest-slack + ingest-magento-modules + ingest-tickets + ingest-email
hooks/                 SessionStart recall + PreCompact consolidation hooks
scripts/               Python helpers used by the ingest commands and hooks (stdlib only)
infra/                 docker-compose recipe for the host-side Neo4j + Graphiti stack
```

Installing the plugin gives Claude the MCP client config + the usage skill. **You still need to stand up the server yourself** (see Graphiti Server).

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

## Bulk ingestion — folders, Slack, Magento modules, GitHub tickets, IMAP email

Five slash commands import external content as Graphiti episodes. All go through the same `add_memory` tool an agent would use ad-hoc; the scripts just batch the calls.

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

### `/pb-graphiti:ingest-email <address> --since YYYY-MM-DD`

IMAP-based email ingest. One episode per email thread (grouped by RFC 2822 References/In-Reply-To, falling back to normalized subject). HTML stripped, quoted replies trimmed, attachments noted in headers only.

Auth: password read from env var (default `$IMAP_PASSWORD`) — never on the CLI. Use an **app password**, not your account password.

#### Project-relevance filtering

Two layers, stack them:

1. **Address allowlist** — `--addresses '@client.com,external-consultant@vendor.com'`. Matches against EVERY participant (`From`, `To`, `Cc`, `Bcc`, `Reply-To`) — including Cc'd parties, so threads where the client is Cc'd alongside an internal recipient still match. Each entry is either a full address or a domain match (`@client.com` covers anyone at that domain). Run client-side after IMAP fetch so coverage is reliable across providers.
2. **Keyword gate** — `--include-keywords 'projectname,ticket-prefix'`. Applied to the rendered thread body (after HTML strip + quote trim). Useful when the address list alone lets in too much chatter — e.g., a client domain that also handles unrelated business.

Both filters AND together (a thread must pass both gates if both are set). Combine an address allowlist with a tight keyword list when correspondence is mixed.

#### Safety: `--require-relevance`

Pass `--require-relevance` to **refuse to run** unless an address allowlist or keyword list is set. Prevents the foot-gun of accidentally ingesting an entire mailbox with no scope. Strongly recommended for first-time runs against a new mailbox.

#### Other filters

`--since YYYY-MM-DD` is a server-side IMAP filter; `--min-words` to drop auto-replies (default 10); `--min-thread-messages` to drop one-offs. Code-entity suppression on by default (same rationale as tickets).

`source_description` is `mid:<Message-ID>` of the thread root — RFC 2392 URI that opens in mail clients respecting the scheme.

Best for: client mailboxes with project-specific subject prefixes, vendor correspondence threads, contract negotiation history.

### `/pb-graphiti:ingest-tickets <since-year>`

GitHub issues and pull requests with their comment threads. Driven by the `gh` CLI (auth via `$GH_TOKEN` or `gh auth login`). One episode per ticket containing title, state, labels, author, body, and chronological comment thread. Bot comments (dependabot, github-actions, codecov) filtered out by default.

The `since` argument accepts a year (`2024`) or full ISO date (`2024-06-15`). Repo defaults to the current git origin; override with `--repo owner/name`. Default labels excluded: `dependencies,duplicate,invalid,wontfix` — override with `--exclude-labels`.

Cap: 30k chars per episode (long discussions truncated). `source_description` is the ticket's `html_url`, so every recalled fact cites back to the exact thread.

**Why this is the highest-value ingest source.** Tickets are where decisions happen: vendor verdicts, design rationale, SEO strategies, bug postmortems. After ingesting, `search_nodes(group_ids=[<project>, "fleet"], query="why X")` typically surfaces the original discussion. Pairs naturally with `ingest-magento-modules`: modules show what was built, tickets show why.

### `/pb-graphiti:ingest-magento-modules [<project-root>]`

Magento-aware module-doc ingest — the recommended path for capturing project module documentation. Walks `app/code/<Vendor>/<Module>/` (and optionally `vendor/*/*/` with `--include-vendor`) and assembles ONE episode per module containing:

- Canonical `Vendor_Module` name (from `etc/module.xml`)
- `<sequence>` dependencies (module-level — these read as docs)
- composer.json: description, version, require, license
- README.md / readme.md content (if present)
- CHANGELOG.md head (last 5 entries, if present)

**Deliberately excluded — GitNexus owns these:** di.xml preferences, plugin targets, events.xml observers. Putting class wiring into Graphiti creates hundreds of Component nodes per project — graph noise that duplicates GitNexus's structural index. Keep the layers clean: GitNexus = code structure (symbols, callers, signatures); pb-graphiti = the *why* (purpose, design rationale, vendor verdicts from READMEs and changelogs).

Each episode's `source_description` is the module's `file://` URI, so recalled facts cite back to the exact module directory.

Modules with no README, no composer description, and no CHANGELOG are skipped — a bare module.xml has nothing for Claude to recall. The dry-run reports the skip count.

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

## Troubleshooting

### Identifying which ingestion channel produced an episode

Episodes don't carry an explicit "ingestion source" field, but every channel writes a distinct `source_description` shape, so you can classify them with a string-prefix check:

| Channel | `source_description` pattern |
|---|---|
| `/pb-graphiti:ingest-folder` | `file:///<absolute-path>` (file URI; no trailing slash on the file itself) |
| `/pb-graphiti:ingest-magento-modules` | `file:///<absolute-path-to-module-dir>/` (trailing slash — module directory) |
| `/pb-graphiti:ingest-slack` (with `--workspace-slug`) | `https://<workspace>.slack.com/archives/<channel-id> (<YYYY-MM-DD>)` |
| `/pb-graphiti:ingest-slack` (no slug) | `slack:<channel>:<YYYY-MM-DD>` |
| `/pb-graphiti:ingest-tickets` | `https://github.com/<owner>/<repo>/issues/<n>` (or `/pull/<n>`) |
| `/pb-graphiti:ingest-email` | `mid:<Message-ID>` |
| PreCompact consolidation hook | `claude-code-session://<session_id> [precompact <YYYY-MM-DD>]` |
| Manual `add_memory` | whatever the caller passed (often opaque) |

### Counting episodes per channel per project

Open the Neo4j browser at `http://localhost:7474` (or use the HTTP API). Cypher:

```cypher
MATCH (ep:Episodic)
WITH ep.group_id AS gid,
     CASE
       WHEN ep.source_description STARTS WITH 'https://github.com/'         THEN 'github-tickets'
       WHEN ep.source_description STARTS WITH 'mid:'                         THEN 'email'
       WHEN ep.source_description STARTS WITH 'claude-code-session://'       THEN 'precompact-hook'
       WHEN ep.source_description STARTS WITH 'https://' AND ep.source_description CONTAINS '.slack.com/' THEN 'slack-permalinked'
       WHEN ep.source_description STARTS WITH 'slack:'                       THEN 'slack-opaque'
       WHEN ep.source_description STARTS WITH 'file://' AND ep.source_description ENDS WITH '/' THEN 'magento-module'
       WHEN ep.source_description STARTS WITH 'file://'                      THEN 'folder-doc'
       ELSE 'other/unknown'
     END AS channel
RETURN gid, channel, count(*) AS episodes
ORDER BY gid, episodes DESC;
```

### Selectively wiping one channel (worked example: GitHub tickets only)

**Use case:** you want to drop all GitHub-ticket episodes from `lcd-mageos` (e.g., to re-ingest with tighter filters) without touching modules, Slack, email, or hook-written facts.

**Step 1 — preview what would be deleted.** Always run this first.

```cypher
MATCH (ep:Episodic)
WHERE ep.group_id = 'lcd-mageos'
  AND ep.source_description STARTS WITH 'https://github.com/'
RETURN count(ep) AS would_delete;
```

If the count looks right, proceed. If it's surprisingly large or small, recheck your `WHERE` clauses before running the delete.

**Step 2 — delete the episodes.**

```cypher
MATCH (ep:Episodic)
WHERE ep.group_id = 'lcd-mageos'
  AND ep.source_description STARTS WITH 'https://github.com/'
DETACH DELETE ep;
```

`DETACH DELETE` removes the episode node AND its `MENTIONS` edges to any extracted entities. Entities themselves are NOT deleted in this step — Graphiti's entities are aggregated across many source episodes, so an entity like a vendor name may still be referenced by module or Slack episodes after the tickets are gone. That's intentional.

**Step 3 — clean up orphaned entities.** After Step 2, some entities may have no remaining `MENTIONS` edges (i.e., they only ever appeared in ticket episodes). Remove them:

```cypher
MATCH (e:Entity)
WHERE e.group_id = 'lcd-mageos'
  AND NOT EXISTS { MATCH (:Episodic)-[:MENTIONS]->(e) }
DETACH DELETE e;
```

This step is safe: it only deletes entities that no surviving episode references, so entities still mentioned by modules/Slack/email survive automatically.

**Step 4 — verify.**

```cypher
MATCH (n)
WHERE n.group_id = 'lcd-mageos'
RETURN labels(n) AS labels, count(*) AS c
ORDER BY c DESC;
```

You should see the Episodic count drop by the ticket count and entity counts reduced by any orphans. Modules / Slack / email episodes and their entities should be untouched.

### Pattern variations for the other channels

Substitute the `STARTS WITH` clause in Step 1 and Step 2:

```cypher
-- Wipe all Slack ingest (both permalinked and opaque variants)
AND (ep.source_description CONTAINS '.slack.com/' OR ep.source_description STARTS WITH 'slack:')

-- Wipe all email ingest
AND ep.source_description STARTS WITH 'mid:'

-- Wipe all Magento-module ingest (file:// URIs ending in /)
AND ep.source_description STARTS WITH 'file://' AND ep.source_description ENDS WITH '/'

-- Wipe all folder-doc ingest (file:// URIs NOT ending in /)
AND ep.source_description STARTS WITH 'file://' AND NOT ep.source_description ENDS WITH '/'

-- Wipe everything written by the PreCompact consolidation hook
AND ep.source_description STARTS WITH 'claude-code-session://'
```

### After a selective wipe, fix the dedupe state file

The `.pb-graphiti-ingest.json` state file in the cwd where you ran the original ingest contains hashes of every successfully-written episode. After wiping that channel from the graph, the next re-ingest will skip everything (dedupe-hits). Either:

- Delete the state file, OR
- Pass `--reingest` on the next ingest run.

Otherwise you'll wipe the graph, re-run the ingest, and see "nothing to do (all up to date)" — confusing.

### Exploring the graph — common Neo4j Browser queries

Open the Neo4j Browser at `http://localhost:7474` (login: `neo4j` / your `NEO4J_PASSWORD`). All queries below scope by `group_id` — change `'lcd-mageos'` to your project id (or `'fleet'`, `'initial_ingest'`).

#### Browser settings worth setting once

Run these in the browser command bar — they persist as Browser settings:

```
:config initialNodeDisplay: 25
:config maxNeighbours: 20
:config maxRows: 500
```

Without these the Browser silently truncates at 1000 nodes on result graphs and 1000 rows on tabular returns. If your project has 10,000+ nodes, you'll see incomplete pictures and not realize it.

#### Project overview — counts by node type

```cypher
MATCH (n) WHERE n.group_id = 'lcd-mageos'
RETURN labels(n) AS type, count(*) AS count
ORDER BY count DESC;
```

#### View a small subgraph for visual exploration

The full project graph is too big to render — start with a small slice and expand by double-clicking nodes in the Browser:

```cypher
// Top 25 most-connected entities (graph "hubs") for this project
MATCH (e:Entity {group_id: 'lcd-mageos'})
OPTIONAL MATCH (e)-[r]-()
WITH e, count(r) AS connections
ORDER BY connections DESC
LIMIT 25
RETURN e;
```

```cypher
// Most-recent 30 episodes — pairs with their extracted entities
MATCH (ep:Episodic {group_id: 'lcd-mageos'})
WITH ep ORDER BY ep.created_at DESC LIMIT 30
OPTIONAL MATCH (ep)-[r:MENTIONS]->(e:Entity)
RETURN ep, r, e;
```

#### List entities by label/type

```cypher
// All Vendors
MATCH (e:Entity:Vendor {group_id: 'lcd-mageos'})
RETURN e.name AS name, e.summary AS summary
ORDER BY e.name;

// All Decisions with their rationale
MATCH (e:Entity:Decision {group_id: 'lcd-mageos'})
RETURN e.name, e.summary
ORDER BY e.created_at DESC;

// All Incidents
MATCH (e:Entity:Incident {group_id: 'lcd-mageos'})
RETURN e.name, e.summary, e.created_at
ORDER BY e.created_at DESC;
```

Available entity labels (from `infra/config/config.yaml` entity_types): `Vendor`, `Decision`, `Incident`, `Project`, `Client`, `Procedure`, `Component`, `Preference`, `Topic`. Plus the generic `Entity` for anything that didn't get a specialized type.

#### Find an entity by name (fuzzy)

```cypher
MATCH (e:Entity {group_id: 'lcd-mageos'})
WHERE toLower(e.name) CONTAINS 'tax'
RETURN labels(e) AS labels, e.name, e.summary
LIMIT 20;
```

#### Trace provenance — for an entity, show the source episodes

```cypher
MATCH (e:Entity {group_id: 'lcd-mageos'})
WHERE e.name = 'Honeycomb'
OPTIONAL MATCH (ep:Episodic)-[:MENTIONS]->(e)
RETURN e.name AS entity, e.summary AS summary,
       collect({name: ep.name, source: ep.source_description, when: ep.created_at}) AS episodes;
```

#### Recently added (last 24 hours)

```cypher
MATCH (ep:Episodic)
WHERE ep.created_at >= datetime() - duration({hours: 24})
RETURN ep.group_id, ep.name, ep.source_description, ep.created_at
ORDER BY ep.created_at DESC;
```

#### Episodes from one channel, scoped to project

```cypher
// All GitHub ticket episodes from lcd-mageos
MATCH (ep:Episodic {group_id: 'lcd-mageos'})
WHERE ep.source_description STARTS WITH 'https://github.com/'
RETURN ep.name, ep.source_description, ep.created_at
ORDER BY ep.created_at DESC
LIMIT 50;
```

#### Two-hop walk — find entities connected to a starting entity

```cypher
// What is "ProxiBlue" connected to in lcd-mageos?
MATCH (start:Entity {group_id: 'lcd-mageos', name: 'ProxiBlue'})-[r1]-(neighbor)-[r2]-(second)
WHERE second.group_id = 'lcd-mageos'
RETURN start, r1, neighbor, r2, second
LIMIT 50;
```

In the Browser this renders as a small subgraph — drag, expand, explore.

#### Storage size sanity check

```cypher
// Episode size distribution per project — useful when chasing token cost
MATCH (ep:Episodic)
WITH ep.group_id AS gid, size(ep.content) AS chars
RETURN gid,
       count(*) AS episodes,
       sum(chars) AS total_chars,
       avg(chars) AS avg_chars,
       max(chars) AS max_chars
ORDER BY total_chars DESC;
```

```cypher
// Top 10 biggest individual episodes (candidates for truncation review)
MATCH (ep:Episodic)
RETURN ep.group_id, ep.name, size(ep.content) AS chars, ep.source_description
ORDER BY chars DESC
LIMIT 10;
```

### A note on node-cap surprises

Neo4j Browser caps rendered graphs at 1000 nodes by default. If you run `MATCH (n {group_id: 'pvcpipesupplies'}) RETURN n` and see exactly 1000 nodes back, that's the cap — your actual count may be higher. Either tighten the query (filter by label, time, name pattern) or raise `:config maxRows` and accept slower render.

For programmatic use (the HTTP API at `/db/neo4j/tx/commit`), there's no implicit cap — `LIMIT` is whatever you write.

## Companion plugins / related work

- [`pb-gitnexus`](https://github.com/proxiblue/pb-gitnexus) — structural code graph (gitnexus) for Magento / Mage-OS. Pairs well: gitnexus = code structure, pb-graphiti = domain knowledge.
- Built-in Claude Code auto-memory — keep using it for hard rules.

## License

MIT — see [LICENSE](./LICENSE).
