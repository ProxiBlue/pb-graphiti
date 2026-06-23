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

1. **Before EVERY `add_memory` call: classify and confirm scope with the user.**
   Output ONE line in this exact form, then wait for confirmation:

   ```
   Save to graph — scope: [fleet | project=<id>]. Reply g / p / correction.
   ```

   - `g` (or `global` / `fleet`) → use `group_id="fleet"`.
   - `p` (or `project`) → use the resolved project id.
   - Any other reply → treat as a correction; re-classify and re-confirm.

   Default suggestion: project, unless the fact references multiple projects, names a tool/methodology, or codifies a policy that applies anywhere. When in doubt → ask.

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
  source_description="claude-code session <date>"
)
```

Searching from inside a project context (always pass BOTH project + fleet):
```
search_nodes(group_ids=["<project-id>", "fleet"], query="<your question>")
search_memory_facts(group_ids=["<project-id>", "fleet"], query="<relationship you need>")
```

Searching from a host shell (no project context):
```
search_nodes(group_ids=["fleet"], query="<your question>")
```

## Why this exists

Flat-file memory at `~/.claude/projects/.../memory/` scales to ~50 facts before the index becomes unreadable. Domain knowledge across a fleet of projects easily hits 500+. Graphiti handles supersession (X was true until time T, then Y) and cross-fact retrieval (entity-relation queries) natively — neither is possible in flat markdown.
