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

There is **one** Neo4j database backing all your projects. Namespacing is per-call, via the `group_id` field on every `add_memory` / `search_memory_*` call.

Two tiers:

- **`group_id="fleet"`** — cross-project facts (methodology, vendor rules, organisation policy).
- **`group_id="<project-id>"`** — project-specific quirks. Resolve from `$DDEV_PROJECT` or `basename $(git rev-parse --show-toplevel)`.

From inside a project, ALWAYS query both: `group_ids: ["<project-id>", "fleet"]`. Surfaces fleet rules everywhere without leaking project A's quirks into project B.

Full discipline (when to write, when to read, scope confirmation rule, cypher to move mis-scoped nodes) lives in [`skills/graphiti-usage/SKILL.md`](./skills/graphiti-usage/SKILL.md). The skill auto-surfaces to Claude when the graphiti MCP is reachable.

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
