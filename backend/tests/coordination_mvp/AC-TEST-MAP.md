# Coordination MVP — AC → Test / Gate Map

Maps every acceptance criterion in `EVENT-SUBSTRATE-ACCEPTANCE.md` (+ the
`PRD-PARENT-REVIEW-GATE` ACs `AC-4.10/4.11`) to the **owning slice**, the **S0
artifact** that makes it measurable today, and the **later test** that must
prove it. S0 ships the fixtures, guards, and xfail scaffolds in the right-hand
columns; S1–S7 turn the scaffolds green.

Legend — S0 artifact:
`fix`=named fixture in `fixtures.py` · `guard`=ratchet in `test_guards.py` ·
`Xn`=cross-slice scaffold in `test_cross_slice_contract.py`.

## AC-0 · Package gate

| AC | Slice | S0 artifact | Later test / gate |
|----|-------|-------------|-------------------|
| AC-0.1 one intake + one delivery path | S1,S4 | guard (no-bespoke-router) | resolve_targets is the single router; no leaf bypass |
| AC-0.2 every case cites a PRD id | all | `fix.acs`, this map | each test references its AC |
| AC-0.3 unsupported leaf listed w/ follow-up | all | PR-MAP "Out of launch" | reviewer checks no silent fallback |
| AC-0.4 reviewer blocks local delivery class / 2nd owner truth | S4 | guard ×3 | the guard ratchets ARE this gate |

## AC-1 · Event Model & connector declarations

| AC | Slice | S0 artifact | Later test |
|----|-------|-------------|------------|
| AC-1.1 stable identity / dedupe | S1 | `fix:github_check_completed` | redeliver → same event_id (`X3` + S1 unit) |
| AC-1.2 registered type required | S1 | — | unregistered type rejects loudly |
| AC-1.3 connector manifest shape | S1 | `fix` (all connectors) | manifest declares facts/identity/relationships |
| AC-1.4 no behavior DSL | S1 | guard (taxonomy) | manifest cannot declare wake/digest/warning |
| AC-1.5 namespace conflict | S1 | — | ambiguous fact mapping rejects at declare time |
| AC-1.6 Gmail example | S1,S3 | `fix:gmail_first_touch/repeat` | `X1`,`X2` |
| AC-1.7 GitHub example | S1,S4 | `fix:github_pr_opened/check_completed` | `X3` |
| AC-1.8 document-comment example | S1,S4 | `fix:document_comment` | `X4` |
| AC-1.9 versioning/replay | S1 | — | replay-retained event reprocesses after fix |

## AC-2 · Owner resolution & triage

| AC | Slice | S0 artifact | Later test |
|----|-------|-------------|------------|
| AC-2.1 artifact owner | S4 | `fix` path_a set | `X2`,`X3`,`X4` |
| AC-2.2 multi-recipient | S4 | `fix:parent_chain_digest_only` | most-specific wake, broader digest |
| AC-2.6 peer-scope conflict | S3 | `fix:peer_conflict_repair` | `X5` (order decides; fan-out audited) |
| AC-2.3 no artifact match → triage | S4 | `fix:delivery_health_sink` | narrowest triage owner |
| AC-2.7 live triage owner | S4 | `fix:delivery_health_sink` | `X10` (archived re-resolves) |
| AC-2.4 no guessing | S4 | guard (no-bespoke-router) | never picks from cwd/title/payload |
| AC-2.5 self/bot suppression | S1,S4 | (existing `guard.py`) | single suppression path, no loop |

## AC-3 · Delivery dispositions

| AC | Slice | S0 artifact | Later test |
|----|-------|-------------|------------|
| AC-3.1 five-disposition taxonomy | S4 | guard (taxonomy) + `fix` | only the 5; user-attention excluded |
| AC-3.2 owner wake | S4 | `fix:github_pr_opened` | reviewer block wakes worker |
| AC-3.3 owner digest | S4 | `fix:gmail_repeat` | context, no turn/unread |
| AC-3.4 subscriber wake (rare) | S4 | — | only declared affected-subscriber |
| AC-3.5 subscriber digest / no wake chain | S4 | `fix:parent_chain_digest_only` | `X6` |
| AC-3.6 dead-letter/failure loud | S4 | `fix:delivery_health_sink` | `X10` |
| AC-3.7 disposition derivation | S4 | guard (taxonomy) | derived, not connector-driven |
| AC-3.8 badge pressure | S4,S6 | — | only approved user-facing artifacts badge |
| AC-3.9 parent disposition route-back | S5 | `fix:review_profile_fresh_run` | `X8` + route-back unit |

## AC-4 · User-raised artifacts & clearing

| AC | Slice | S0 artifact | Later test |
|----|-------|-------------|------------|
| AC-4.1 requires-user surface | S6 | `fix:raised_artifact_closeout` | artifacts not buried chat |
| AC-4.2 explicit clearing | S6 | `fix:raised_artifact_closeout` | close-viewer ≠ clear |
| AC-4.3 clearing emits | S6 | `fix:raised_artifact_closeout` | `X7` |
| AC-4.4 cleared-without-answer | S6 | — | owner gets `cleared_without_answer` |
| AC-4.5 defer-until | S7 | `fix:defer_until_deadline_expiry` | `X9` |
| AC-4.6 deliverables | S5,S6 | — | promotes when complete + reviewed |
| AC-4.7 solid content surface | S6 | — | UI: not glass |
| AC-4.8 one row per request | S6 | — | UI: ordered by urgency |
| AC-4.9 decision confidence | S6 | — | calibrated confidence shown |
| AC-4.10 Artifact Review Profile | S5 | `fix:review_profile_fresh_run` | `X8` (fresh context) |
| AC-4.11 profile-constrained disposition | S5 | `fix:review_profile_fresh_run` | `X8` (blocking → no approve) |
| AC-4.12 archive vs subscription archive | S2,S6 | `fix:raised_artifact_closeout` | `X7` (closeout ≠ archive) |

## AC-5 · Expected events, liveness, restart

| AC | Slice | S0 artifact | Later test |
|----|-------|-------------|------------|
| AC-5.1 expected-event watch | S7 | `fix:defer_until_deadline_expiry` | `X9` |
| AC-5.2 missing reviewer/CI | S7 | — | armed watch → wake on no-show |
| AC-5.3 plan idle distinction | S7 | — | waiting vs idle-incomplete |
| AC-5.4 bounded deferral | S7 | — | repeated defer escalates |
| AC-5.5 startup re-arm | S7 | — | no dormant automation after start |
| AC-5.6 restart drain | S7 | — | registered work woken post-restart |
| AC-5.7 idempotency | S7 | — | retry/restart no double-fire |
| AC-5.8 re-raise bound | S6,S7 | — | clear/re-raise loop bounded |
| AC-5.9 shared event identity | S1 | `fix:github_check_completed` | one id across intake/delivery/automation |
| AC-5.10 replay-retained bounds | S1 | — | replay throttled + bounded |

## AC-6 · Visibility, observability, health

| AC | Slice | S0 artifact | Later test |
|----|-------|-------------|------------|
| AC-6.1 delivery observability | S4 | — | surface shows disposition |
| AC-6.2 intake health | S1 | — | accepted/rejected/dup/replay metrics |
| AC-6.3 automation health | S7 | — | armed/fired/failed/unresolved metrics |
| AC-6.4 artifact attention health | S2,S6 | — | attention-state distinct from archive |
| AC-6.5 routing telemetry discriminator | S1 | — | telemetry non-routable unless promoted |

## Gate summary

- **S0 gate (this PR):** `tests/coordination_mvp/` green = guards pass + all
  fixtures self-validate + every cross-slice item has a scaffold. `git diff --check`.
- **Per later-slice gate:** the slice removes the `xfail` for its `Xn`
  scaffold(s) and adds the unit/integration tests in the empty "later test"
  cells above; the three guards stay green.
- **Cross-slice gate (every impl PR before "ready"):** all 10 `Xn` for landed
  slices pass; the 3 guards green.
