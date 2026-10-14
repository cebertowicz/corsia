/**
 * Decoder for the operator envelope (`backend/plans/operator_projection.py`).
 *
 * A 2xx body is trusted only after it is proven to be the contract: every
 * field a template reads before it can branch is checked, nested ones
 * included. Anything else decodes to null and the read is reported as a
 * failed one -- never "Unavailable" rendered from undefined, never a throw
 * inside a consumer. Every operator read passes through this (`plans.ts`).
 */
import type { OperatorEnvelope, OperatorNode } from './plansTypes';

const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === 'string';
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isStrOrNull = (v: unknown): v is string | null => v === null || isStr(v);
const isNumOrNull = (v: unknown): v is number | null => v === null || isNum(v);
const isStrArray = (v: unknown): v is string[] => Array.isArray(v) && v.every(isStr);
const isDisplay = (v: unknown): boolean => isObject(v) && isStr(v.label_key) && isStr(v.tone_key);
const isProgress = (v: unknown): boolean => isObject(v) && isNum(v.done) && isNum(v.total);
const isPlanProgress = (v: unknown): boolean => isProgress(v) && isNum((v as Record<string, unknown>).open);
const isChild = (v: unknown): boolean =>
	v === null || (isObject(v) && v.available === true && isStr(v.plan_id) && isStr(v.thread_id)
		&& isPlanProgress(v.progress) && isStr((v.progress as Record<string, unknown>).liveness));
const isReview = (v: unknown): boolean => v === null || (isObject(v) && isStr(v.kind) && isStr(v.label) && isObject(v.actor));
const isOversize = (v: unknown): boolean => v === undefined || v === null || (isObject(v) && isNum(v.open) && isNum(v.budget));
const isCause = (v: unknown): boolean => isObject(v) && isStrOrNull(v.reason) && isStr(v.owner_kind);
const isAttempt = (v: unknown): boolean =>
	isObject(v) && isNum(v.ordinal) && isObject(v.executor) && isStr(v.executor.label)
		&& isStrOrNull(v.released_at) && isStrOrNull(v.release_reason);
const isExecution = (v: unknown): boolean =>
	isObject(v) && isStr(v.condition) && isStrOrNull(v.primary_reason) && isNum(v.attempt_count)
		&& isNum(v.friction_count) && (v.revision_round === undefined || isNum(v.revision_round))
		&& Array.isArray(v.causes) && v.causes.every(isCause)
		&& Array.isArray(v.attempts) && v.attempts.every(isAttempt);
/** A measured total: `known` is null until something was measured. */
const isMeasure = (v: unknown): boolean => isObject(v) && isNumOrNull(v.known) && isNum(v.unknown_rows);
const isAmount = (v: unknown): boolean => {
	if (!isObject(v) || !isMeasure(v.tokens) || !isMeasure(v.model_cost_usd)) return false;
	if (!isNum(v.usage_recording_failures) || !isObject(v.execution)) return false;
	if (!isObject(v.waiting) || !isNum(v.waiting.seconds)) return false;
	const execution = v.execution as Record<string, unknown>;
	return isNum(execution.seconds) && isNum(execution.unknown_turns) && isObject(execution.turns);
};
const isNodeCost = (v: unknown): boolean =>
	isObject(v) && isAmount(v.own) && isAmount(v.rolled);
const isInitiative = (v: unknown): boolean =>
	v === null || (isObject(v) && isNum(v.follow_ups) && isAmount(v.cost) && typeof v.open === 'boolean');
const isCommitmentCost = (v: unknown): boolean =>
	v === null || (isObject(v) && isStr(v.commitment_id) && isObject(v.attempts) && isNum(v.attempts.total)
		&& isMeasure(v.tokens) && isMeasure(v.model_cost_usd) && isNum(v.usage_recording_failures)
		&& isObject(v.elapsed_seconds)
		&& isNumOrNull(v.elapsed_seconds.known) && isObject(v.user_effort) && isNum(v.user_effort.known_plan_actions));
const isEvidence = (v: unknown): boolean =>
	v === null || (isObject(v) && isStr(v.evidence) && isStrArray(v.artifact_ids));

function isNode(v: unknown): boolean {
	if (!isObject(v) || !isStr(v.id) || !isStrOrNull(v.parent_id) || !isNum(v.depth)) return false;
	if (!isStr(v.kind) || !isStr(v.title) || !isStr(v.state) || !isNum(v.revision)) return false;
	if (!isStrArray(v.unmet_dependencies) || !isStrOrNull(v.held_by)) return false;
	if (v.assignee !== undefined && !isStrOrNull(v.assignee)) return false;
	if (typeof v.folded !== 'boolean' || !isStr(v.last_transition_at)) return false;
	if (!isObject(v.executor) || !isStr(v.executor.label) || !isExecution(v.execution) || !isDisplay(v.display)) return false;
	if (v.status_reason !== undefined && !isStrOrNull(v.status_reason)) return false;
	if (!isStrOrNull(v.scheduled_until)) return false;
	if (!isChild(v.child) || !isReview(v.review) || !(v.branch_progress === null || isProgress(v.branch_progress))) return false;
	if (!isCommitmentCost(v.commitment_cost) || !isNodeCost(v.node_cost) || !isInitiative(v.initiative)) return false;
	if (!isNum(v.requirements_revision) || !isEvidence(v.evidence)) return false;
	return Array.isArray(v.user_actions) && isStrArray(v.attention_path);
}

/** One operator node (the single-node read), or null when the body is not one. */
export function decodeOperatorNode(v: unknown): OperatorNode | null {
	return isNode(v) ? (v as unknown as OperatorNode) : null;
}

const isFoldLevel = (v: unknown): boolean =>
	isObject(v) && isStrOrNull(v.parent_id) && isNum(v.done) && isNum(v.failed) && isNum(v.closed) && isNum(v.more);

function isPlanEntry(v: unknown): boolean {
	if (!isObject(v) || !isObject(v.plan) || !isStr(v.plan.id) || !isStr(v.plan.state) || !isNum(v.plan.revision)) return false;
	if (!isObject(v.owner) || !isObject(v.progress) || !isPlanProgress(v.progress)) return false;
	if (!isObject(v.progress.distribution) || !isDisplay(v.display) || !isOversize(v.oversize)) return false;
	if (!isStrArray(v.current_node_ids) || !isStrArray(v.next_node_ids)) return false;
	if (!Array.isArray(v.folds) || !v.folds.every(isFoldLevel) || !isNum(v.root_count)) return false;
	return Array.isArray(v.nodes) && v.nodes.every(isNode);
}

/** The operator envelope, or null when the body is not one. */
export function decodeOperatorEnvelope(v: unknown): OperatorEnvelope | null {
	if (!isObject(v) || v.contract_version !== 2 || !isStr(v.as_of) || !isStr(v.revision_token)) return null;
	if (!isObject(v.target) || !isStr(v.target.kind) || !isStr(v.target.id) || !isStr(v.target.label)) return null;
	if (!isObject(v.attention) || !isNum(v.attention.awaiting_count)) return null;
	if (v.seen_through !== undefined && !isStrOrNull(v.seen_through)) return null;
	if (!Array.isArray(v.plans) || !v.plans.every(isPlanEntry)) return null;
	return v as unknown as OperatorEnvelope;
}
