# CORSIA QA Adapter

This directory contains CORSIA-specific knowledge for the generic QA core in `qa/core`.

- `surfaces.yml` is the versioned feature and behavior catalog.
- `impact-map.json` maps changed files to deterministic test commands.
- `ux-review-prompt.md` is the local multimodal review rubric.
- `tool-candidates.json` captures the OSS/source-available evaluation matrix.

Useful commands from the repo root:

```bash
node qa/core/cli.mjs select
node qa/core/cli.mjs catalog --write
node qa/core/cli.mjs eval-tools
node qa/core/cli.mjs run --dry-run
node qa/core/cli.mjs review --run <run-id>
```

The compatibility wrapper still works:

```bash
node qa/select-tests.mjs --json
```
