---
description: Ingest Magento 2 / Mage-OS module documentation into Graphiti. One episode per module from app/code/<Vendor>/<Module>/, combining module.xml + composer.json + README + CHANGELOG + a terse wiring summary (di preferences / plugin targets / observed events). Pairs with GitNexus — GitNexus owns code structure, Graphiti owns the "why".
argument-hint: [<path-to-magento-project>] [--include-vendor] [--dry-run]
---

You are about to ingest a Magento project's module documentation into Graphiti. Follow this procedure.

## Step 1 — resolve project root + group_id

- `$ARGUMENTS` if non-empty → that's the project root. Otherwise use `$PWD` (the user's current directory).
- The group_id MUST be the project id, not 'fleet' (modules are per-project artefacts):
  - prefer `$DDEV_PROJECT` env var
  - else `basename $(git -C "<project-root>" rev-parse --show-toplevel)`

Confirm with the user in ONE line:

```
Ingest modules from <project-root> — write as group_id=<id>. Proceed? y/n
```

Wait for confirmation. If the user wants a different group_id (e.g., a client-scoped namespace), accept the correction.

## Step 2 — dry-run

Show the plan before writing. Include `vendor/*/*/` modules only if the user explicitly asks for them — `app/code` alone is almost always the right scope.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_magento_modules.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --project-root "<from-step-1>" \
  --dry-run
```

Show the user: module count, group_id, first ~15 module names + sizes. If counts look wrong (zero modules detected, or implausibly many), check the project root. `app/code` must exist under it.

Heads-up the cost: each module is one Anthropic Haiku entity-extraction call. 30-50 modules ≈ $0.05-0.15.

Wait for confirmation: "proceed? y/n".

## Step 3 — write

Same command, no `--dry-run`. The script flushes its dedupe state after every successful write — Ctrl-C is safe, resume by re-running.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_magento_modules.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --project-root "<from-step-1>"
```

Report the final summary (`done: wrote N, failed M`).

## What each episode looks like

The script assembles ONE episode per module containing:

- **Canonical name** from `etc/module.xml` (e.g. `ProxiBlue_Foo`)
- **Relative path** under the project root
- **Source URI** (`file:///full/path/to/module/`) so recalled facts cite back to the module directory
- **setup_version** from module.xml (if set)
- **Dependencies** from `<sequence>` declarations
- **composer.json fields**: description, version, require, type, license
- **README content** (if `README.md` or `readme.md` exists at module root)
- **CHANGELOG head** (last ~5 entries from `CHANGELOG.md` if present)
- **Wiring summary** — *headlines only, NOT full XML*:
  - DI preferences (which classes the module overrides)
  - Plugin targets (which classes it intercepts)
  - Events it observes (frontend + adminhtml + base scopes merged)

The wiring summary is intentionally terse — GitNexus indexes the structural detail. This episode captures the *intent* of the module's wiring so recall can answer "does this project have a module that overrides X?" or "what observes the sales_order_save_before event in project Y?".

## Notes

- Modules with no README, no composer.json, no wiring, and a bare module.xml produce only ~80 characters and are SKIPPED (`build_episode` returns None). The dry-run summary shows the skipped count.
- The script tries `etc/module.xml` first, then `etc/frontend/` and `etc/adminhtml/` for area-scoped events.xml / di.xml. All merged into one episode per module.
- For composer-installed third-party modules, pass `--include-vendor`. Default is OFF because vendor modules usually have well-known docs elsewhere and would explode the episode count.
- Dedupe state file: `.pb-graphiti-ingest.json` in cwd. Module hash includes the full episode body — if any of its sources change, the new version writes (without removing the old episode). To rebuild from scratch, pass `--reingest`.
