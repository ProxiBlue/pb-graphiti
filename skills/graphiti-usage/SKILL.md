---
name: graphiti-usage
description: Discipline for writing to and reading from the shared Graphiti temporal knowledge graph. Use when adding cross-session facts, recalling decisions/incidents/vendor verdicts, or wiring per-project namespacing via group_id. Applies whenever the graphiti MCP server is reachable.
---

# Graphiti Knowledge Graph — usage discipline

Shared temporal knowledge graph reached via the `graphiti` MCP server (default `http://host.docker.internal:8765/mcp/`). One host instance backs all projects; namespacing is enforced **per call** via `group_id` — there is no automatic project isolation.

## Scope model — two tiers

Every fact has a SCOPE. Pick at write time:

| Scope | `group_id` value | What goes here |
|---|---|---|
| **fleet** | the literal string `"fleet"` | Cross-project rules, tool-use defaults, methodology, vendor decisions that apply anywhere, organisation-wide policy |
| **project** | the project's stable id (e.g. `myapp`, `client-x-store`) | Project-specific quirks: LIVE-equivalent branch, project-only test commands, module-vendor decisions that don't generalise, per-client preferences |

Resolve the project id deterministically:
- DDEV: read `$DDEV_PROJECT` env var inside the container (set automatically).
- Generic: `basename $(git rev-parse --show-toplevel)`.
- Never use `default`, `main`, session ids, or random suffixes.

## Hard rules

1. **Before EVERY in-conversation `add_memory` call: classify and confirm scope with the user.**
   Output ONE line in this exact form, then wait for confirmation:

   ```
   Save to graph — scope: [fleet | project=<id>]. Reply g / p / correction.
   ```

   - `g` (or `global` / `fleet`) → use `group_id="fleet"`.
   - `p` (or `project`) → use the resolved project id.
   - Any other reply → treat as a correction; re-classify and re-confirm.

   Default suggestion: project, unless the fact references multiple projects, names a tool/methodology, or codifies a policy that applies anywhere. When in doubt → ask.

   **Exception:** the `PreCompact` consolidation hook shipped with this plugin runs non-interactively (no user is at the keyboard to confirm). It resolves scope deterministically from `$DDEV_PROJECT` → git toplevel basename → `fleet`, and writes without prompting. Hard Rule 1 applies to add_memory calls Claude makes mid-conversation, NOT to that hook.

2. **Always query BOTH scopes from a project context.** Use `group_ids: ["<project-id>", "fleet"]` on every `search_nodes` / `search_memory_facts` call. Surfaces fleet rules in every project without leaking project A's quirks into project B.

3. **From the host shell (no project context):** query `group_ids: ["fleet"]` only. Host sessions shouldn't see any project's local quirks unless the user names a project.

4. **Never query across all projects without explicit instruction.** A multi-project audit ("look across everything") is the only valid reason to omit the `group_ids` filter or enumerate every project's id.

5. **Graphiti is for DOMAIN facts. Not for hard rules.** Hard rules that MUST auto-load every session belong in `CLAUDE.md` / `~/.claude/projects/.../memory/`. Graphiti is queried on-demand, not auto-loaded.

## Fixing a wrong scope after the fact

If a node landed in the wrong group_id, fix it via the Neo4j browser at `http://localhost:7474`:

```cypher
// Move ONE specific fact by name (preferred — avoids dragging unrelated nodes)
MATCH (n) WHERE n.group_id = '<wrong-id>' AND n.name CONTAINS '<keyword>'
SET n.group_id = '<right-id>'
RETURN n.name, n.group_id;

// Move the relationships attached to a moved node too
MATCH ()-[r]-(n {group_id: '<right-id>'}) WHERE r.group_id = '<wrong-id>'
SET r.group_id = '<right-id>';

// Move the episode that sourced the fact (so provenance stays consistent)
MATCH (ep:Episodic)-[:MENTIONS]->(n {group_id: '<right-id>'})
WHERE ep.group_id = '<wrong-id>'
SET ep.group_id = '<right-id>';
```

Verify before/after with `MATCH (n) RETURN DISTINCT n.group_id, count(*) ORDER BY count(*) DESC`.

## When to WRITE to Graphiti

Write an episode when you learn ANY of:
- A decision with rationale ("we squash-merge to live because rollback is one `git revert`")
- A non-obvious project quirk ("project X's LIVE-equivalent branch is `uat`, not `main`")
- An incident root cause ("DISPLAY var lost during sweep on <date>; carrier moved to mounts.yaml")
- A vendor / module verdict ("Vendor Y blocked", "Module Z OK for this stack")
- A client preference ("invoices always DRAFT", "PR comments minimised as off-topic")
- A repeatable runbook step ("regenerate index after dependency update")

Don't write:
- Ephemeral session state, in-progress task lists
- Information directly readable from `git log` / `git blame` / current file state
- Restatements of CLAUDE.md content

## When to READ from Graphiti

Read at the start of any task that touches:
- A project area you haven't touched in this session (`search_nodes` with the area name)
- A vendor / module / extension before recommending it
- A branching / deploy / merge step (check for project-specific rules)
- A decision that looks like it might already have a precedent

Cite the Graphiti episode UUID + summary when acting on a recalled fact, same as artefact citation in any investigation protocol.

## Tool call shape

Adding an episode (AFTER scope confirmation per Hard Rule 1):
```
add_memory(
  group_id="fleet",                                # or the resolved project id
  name="<short title>",
  episode_body="<the fact, with Why + How to apply>",
  source="text",
  source_description="claude-code-conversation://<session_id> [add_memory <YYYY-MM-DD>]"
)
```

**Source-description tagging is mandatory for ad-hoc writes.** Use the exact format above:
- Scheme `claude-code-conversation://` distinguishes ad-hoc in-session writes from PreCompact hook writes (which use `claude-code-session://`) and from bulk ingest (which uses `file://`, `https://github.com/`, `mid:`, etc.).
- `<session_id>` is the current Claude Code session's id — available as `$CLAUDE_SESSION_ID` in shell context, or via the session metadata. If unavailable, use the literal string `unknown`.
- The bracketed `[add_memory <date>]` suffix tells operators when and how the fact was written, which is essential for auditing "what has Claude added to the graph this week" — see the README "Auditing automatic writes" section for the cypher query that surfaces these.

Searching from inside a project context (always pass BOTH project + fleet):
```
search_nodes(group_ids=["<project-id>", "fleet"], query="<your question>")
search_memory_facts(group_ids=["<project-id>", "fleet"], query="<relationship you need>")
```

Searching from a host shell (no project context):
```
search_nodes(group_ids=["fleet"], query="<your question>")
```

## Citation discipline — surface the source

Every episode written via the bundled ingest scripts carries a `source_description` that's a real link or path back to the source content:

| Source | `source_description` format |
|---|---|
| Folder ingest (markdown/text/runbook) | `file:///absolute/path/to/file.md` |
| Slack ingest (with `--workspace-slug`) | `https://<workspace>.slack.com/archives/<channel-id> (<YYYY-MM-DD>)` plus per-message permalinks inline in episode body |
| Slack ingest (no workspace slug) | `slack:<channel>:<YYYY-MM-DD>` (informational only — not clickable) |
| Email ingest | `mid:<Message-ID>` (RFC 2392 URI) |
| GitHub tickets ingest | `https://github.com/<owner>/<repo>/issues/<n>` (or `/pull/<n>`) |
| Magento modules ingest | `file:///absolute/path/to/module-dir/` (trailing slash) |
| **PreCompact consolidation hook** | `claude-code-session://<session_id> [precompact <YYYY-MM-DD>]` |
| **TaskCompleted consolidation hook** | `claude-code-session://<session_id> [task-completed <task-id> <YYYY-MM-DD>]` |
| **Ad-hoc Claude `add_memory` in-conversation** | `claude-code-conversation://<session_id> [add_memory <YYYY-MM-DD>]` (per the format above) |
| Manual user `add_memory` | whatever the caller passed |

**When you recall a fact and act on it, surface the source.** Append the `source_description` in brackets after the claim — same discipline as artefact citation in the investigation protocol.

Example (good):
> The fleet decided to ship the new checkout flow next Tuesday because the A/B test showed 4.2% lift at p<0.01 [src: file:///home/lucas/workspace/.../meeting-transcript.md].

Example (bad — claim with no source):
> The fleet decided to ship next Tuesday because of the A/B test results.

**To fetch fuller source content for a recalled entity**, use `get_episodes` filtered by the entity's `episodes` field (returned in some search responses), or query by `group_ids` + a content keyword.

## Pinned (always-loaded) facts

The plugin's SessionStart hook also loads any episodes written to `group_id="initial_ingest"` — capped at the most recent 20 — and surfaces them BEFORE the dynamic top-N recall. Use this group for facts you want Claude to see every session regardless of project context:

- Fleet-wide hard rules (caveman style, billing DRAFT-only, etc.)
- Universal client preferences
- North-star reminders

To pin a fact, call `add_memory(group_id="initial_ingest", ...)`. To unpin, delete the episode via `delete_episode`. There is no auto-pruning — when the group exceeds 20 episodes, the SessionStart hook silently shows only the 20 most recent, so older pins effectively "fall off". Keep the pinned set curated.

## Why this exists

Flat-file memory at `~/.claude/projects/.../memory/` scales to ~50 facts before the index becomes unreadable. Domain knowledge across a fleet of projects easily hits 500+. Graphiti handles supersession (X was true until time T, then Y) and cross-fact retrieval (entity-relation queries) natively — neither is possible in flat markdown.
