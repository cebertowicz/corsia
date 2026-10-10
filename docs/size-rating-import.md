# Importing size ratings

`backend/scripts/size_rating_import.py` records offline size-class ratings of
accepted outcomes as `measurement.size_rating` facts. Measurement's
"Human-hours delivered per hour of your time (estimate)" headline reads them.

## Targets are explicit

Every run names its database, user and scope. Nothing falls back to a default
database. Run from the repository root with Python 3.11 and the backend's
locked dependencies available:

```bash
python3.11 backend/scripts/size_rating_import.py \
  --db /path/to/disposable/corsia.db \
  --user fixture-owner --project all --agent none \
  --input ratings.jsonl            # dry run: reads only, writes nothing
# After reviewing the dry-run result, repeat the same explicit targets with --apply.
```

Without `--apply` the run opens the database read-only and reports what it
would record and what it would refuse. With `--apply` it writes in one
transaction. Rehearse against a copy before pointing it at a live database.

## Input

One JSON object per line, with all 14 fields that
`backend/core/size_rating_import.py` validates: the outcome reference, its
snapshot, the size class (`XS` to `XXL`, or `unknown`), the basis
(`delivered_evidence_proxy` or `accepted_scope_proxy`), and the method, rubric
and rater with their versions. A rater whose version is not known records the
literal `unknown`, never a guess; the headline then marks those weeks not
comparable.

| Fields | Meaning |
|---|---|
| `node_id`, `plan_id`, `node_revision`, `evidence_event_id` | Current canonical task identity; use `none` when no qualifying evidence event exists. |
| `source_snapshot_sha256`, `rater_input_sha256` | Current raw snapshot hash and the exact input hash used by the rater. |
| `method`, `method_version`, `rubric_version`, `rubric_sha256` | Rating procedure and pinned rubric. |
| `rater`, `rater_version`, `basis`, `size_class` | Rater provenance, evidence basis and class; `unknown` class contributes no hours. |

Build the snapshot from the outcome as it stood when it was rated, as
`current_snapshots` in `backend/core/size_rating.py` computes it. A later change to the outcome makes the
rating stale, and it is excluded rather than counted.

## Refusals and retries

One bad line refuses the whole input, and nothing is written; the report
lists every problem. A rating the node's newest fact already serves is
idempotent: rerunning the same file records nothing new and preserves that
observation time. Any other valid rating, including an earlier one re-imported
after a re-rating or a retraction, adds a new fact; it never overwrites,
deletes or revives an old one. Measurement selects the
newest fact, even when that fact is unknown, stale, malformed or withdrawn.
Distinct payloads tied at the newest observation time are a conflict and supply
no estimated hours.

## Synthetic example

The tests in `backend/tests/test_size_rating.py` build complete synthetic rows
for a `fixture-owner` user; copy their shape rather than real data.
