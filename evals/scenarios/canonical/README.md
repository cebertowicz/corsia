# Canonical Scenarios

This directory holds the YAML scenario library consumed by CORSIA's evals
harness. Scenarios are executable evidence, not product authority: current
behavior comes from current PRDs/acceptance criteria and the implemented
runtime. Archived subsystem specs and decisions are historical input only.

---

## Format

Each `.yaml` file is a single scenario. The loader is
`backend/eval/datasets.py:from_yaml_scenario`, which converts the YAML
dict into an Inspect AI `Sample`. The contract is:

| Field | Required | Notes |
|-------|----------|-------|
| `id` | yes | unique scenario id; conventionally `S<n>-<slug>` |
| `name` | yes | human-readable title |
| `description` | recommended | multi-line context — what is being asserted and why |
| `category` | recommended | `functional`, `perf`, `quality`, etc. |
| `tags` | recommended | labels for later filtering |
| `subsystems` | recommended | list of subsystem slugs the scenario covers |
| `mode` | recommended | `functional_behavior` for these scenarios (no default scorer mix) |
| `pre_conditions` | optional | list of fixture invocations from `backend/eval/fixtures/` |
| `steps` | optional | sequence of actions the solver drives — `user_send`, `wait_for_event`, `wait_ms`, etc. |
| `expected_outcomes` | recommended | human-readable assertions; the `scorers:` field is the machine-readable form |
| `scorers` | yes | list of scorer references (see below) |
| `pass_if` | recommended | composition over scorer outcomes (`all_of`, `any_of`) |
| `notes` | optional | free-form |

Either `input:` (a single prompt) OR `steps:` (a step list) is required.
For Mode A scenarios in this directory, `steps:` is the dominant pattern
because the test drives a multi-action turn.

### Scorer references

`scorers:` is a list. Each entry is either:

- A plain string: the scorer name with no args (e.g. `null_tool_paired`).
- A dict: `{"name": "<scorer_name>", "args": {...}}`.

The loader folds `scorers:` into `Sample.metadata['scorers']` so
`backend/eval/scorers/resolve.py:resolve_per_sample_scorers` finds it
without re-reading the YAML.

The canonical scorer registry lives at `backend/eval/scorers/resolve.py`
(`SCORER_REGISTRY`). Slice 2 of this sprint adds 6 functional scorers
that Slice-3 scenarios reference:

- `check_event_present`
- `check_event_metadata_relation`
- `check_no_provider_error`
- `check_assistant_message_content`
- `check_history_contains`
- `check_event_iteration_window`

Plus existing slice-1 scorers: `seam_latency`, `null_tool_paired`,
`structured_validity`. Until Slice 2 lands, the new functional scorers
are `None` stubs in the registry — referenced scorers that are `None`
are silently dropped by `resolve_per_sample_scorers`, so the scenarios
load cleanly but produce a partial scorer set.

---

## Mode A vs Mode B

| Mode | What | Where used | Sprint introduced |
|------|------|------------|-------------------|
| **Mode A** | Mocked LLM via `mock_llm_replies` fixture; deterministic asserts on event sequence and message shape | All Sprint-3 canonical scenarios | Sprint 1 |
| **Mode B** | Real local oMLX inference, scoring TTFT / cache-hit-rate / throughput in addition to functional asserts | Sprint 4 onward | Sprint 4 |

Mode A is functional + behavioral; Mode B layers performance scoring on
top of functional scoring (one scenario, more scorers). The split is
*not* "one is real, one is mocked" — both are real CORSIA code paths;
only the LLM substrate differs.

This directory's Sprint-3 scenarios are all Mode A. When Sprint 4 lands
the first Mode B scenario, the convention will be: same directory, an
extra `mode: real_local` field at the top, and a different fixture set
(no `mock_llm_replies`).

---

## Adding a scenario

1. Name the current PRD/acceptance criterion or concrete regression that the
   scenario proves. Do not create an ADR or subsystem spec for the scenario.
2. Author the YAML here as `<scenario-id>.yaml`.
3. Verify it loads:
   ```bash
   cd backend && ~/.corsia/.venv/bin/python -c "
   import sys, yaml
   from pathlib import Path
   sys.path.insert(0, '.')
   from eval.datasets import from_yaml_scenario
   for p in Path('../evals/scenarios/canonical').glob('*.yaml'):
       from_yaml_scenario(yaml.safe_load(p.open()))
       print('OK', p.name)
   "
   ```
---

## Where the loader lives

- `backend/eval/datasets.py:from_yaml_scenario` — YAML → Inspect AI `Sample`.
- `backend/eval/scorers/resolve.py:resolve_per_sample_scorers` — Sample → scorer callable list.
- `backend/eval/solvers/yaml_scenario.py` — drives the scenario steps against `POST /api/eval/turn` (Slice 1 deliverable, may be in flight at the moment Slice 3 lands its scenarios).
- `backend/eval/fixtures/__init__.py` — `FIXTURES` registry that maps
  `pre_conditions: - fixture: <name>` to a Python coroutine.

---

## Cross-references

- **Current authority map:** `ARCHITECTURE.md`.
- **Sprint 1 reference scenarios:**
  `sprints/2026-05-06-quality-foundation-sprint-01/audit-runs/session-02-scenario-format/scenarios/E*.yaml`
  — the prior art this directory inherits from. E2/E4/E5/E6/E13 belong
  to Sprint-1 dogfood and stay where they are; this directory is the
  canonical home going forward.
