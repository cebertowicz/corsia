import type { OperatorCostAmount, OperatorExecutionCause, OperatorNode } from '$lib/api/plans';
import { formatCost } from '$lib/stores/costs';

/**
 * Who moves a waiting node next, in the reader's words. An agent moving its own
 * work is the default and goes unsaid; only someone else is worth a line.
 */
const NEXT_MOVE: Partial<Record<OperatorExecutionCause['owner_kind'], string>> = {
	user: 'You',
	external: 'Outside CORSIA',
	system: 'CORSIA',
};

/**
 * Plain words for the reasons `plans/admission_attempts.release` callers pass.
 * Some callers pass their own reason, so a reason missing here still shows,
 * as its own words, rather than hiding behind a generic ending.
 */
const TRY_ENDINGS: Record<string, string> = {
	failed: 'released after failure',
	review_rejected: 'review sent it back',
	review_requested: 'sent for review',
	accepted: 'accepted',
	closed: 'closed',
	archived: 'archived',
	requirements_changed: 'requirements changed',
	executor_changed: 'moved to another worker',
	dependency_blocked: 'stopped for a prerequisite',
	watch_armed: 'set aside to wait',
	paused: 'paused',
	blocked: 'blocked',
	spawned_thread_attached: 'handed to a worker',
	selection_invalidated: 'set aside',
	turn_terminal: 'run ended',
};

const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });

export function nextMove(causes: OperatorExecutionCause[]): string | null {
	const owners = [...new Set(
		causes
			.filter((cause) => cause.owner_kind !== 'thread')
			.map((cause) => NEXT_MOVE[cause.owner_kind] ?? cause.owner_kind),
	)];
	return owners.length ? owners.join(', ') : null;
}

export function tryEnding(releasedAt: string | null, reason: string | null): string {
	if (!releasedAt) return 'current';
	if (!reason) return 'ended';
	return TRY_ENDINGS[reason] ?? reason.replaceAll('_', ' ');
}

export function duration(totalSeconds: number): string {
	const days = Math.floor(totalSeconds / 86_400);
	const hours = Math.floor((totalSeconds % 86_400) / 3_600);
	const minutes = Math.floor((totalSeconds % 3_600) / 60);
	const seconds = totalSeconds % 60;
	if (days > 0) return `${days}d ${hours}h`;
	if (hours > 0) return `${hours}h ${minutes}m`;
	if (minutes > 0) return `${minutes}m ${seconds}s`;
	return `${seconds}s`;
}

export function tokens(value: number): string {
	return `${compact.format(value)} tokens`;
}

/** A measured fact stays visibly partial when any matching usage was not recorded. */
function lowerBound(value: string, incomplete: boolean): string {
	return incomplete ? `at least ${value}` : value;
}

/** Tokens are the stable subscription-backed usage fact shown on every detail row. */
export function tokenLine(amount: OperatorCostAmount): string | null {
	if (amount.tokens.known !== null) {
		return lowerBound(
			compact.format(amount.tokens.known),
			amount.tokens.unknown_rows > 0 || amount.usage_recording_failures > 0,
		);
	}
	return amount.tokens.unknown_rows > 0 || amount.usage_recording_failures > 0
		? 'not recorded'
		: null;
}

/** Dollar cost is omitted when no truthful priced amount exists. */
export function dollarLine(amount: OperatorCostAmount): string | null {
	const known = amount.model_cost_usd.known;
	if (known === null || known <= 0) return null;
	return lowerBound(
		formatCost(known),
		amount.model_cost_usd.unknown_rows > 0 || amount.usage_recording_failures > 0,
	);
}

/** Compact combined wording retained for revision/follow-up totals. */
export function costLine(amount: OperatorCostAmount): string | null {
	const parts = [dollarLine(amount)];
	if (amount.tokens.known !== null) {
		parts.push(lowerBound(
			tokens(amount.tokens.known),
			amount.tokens.unknown_rows > 0 || amount.usage_recording_failures > 0,
		));
	}
	return parts.filter((part): part is string => part !== null).join(' · ') || null;
}

/** Time spent in model Turns, separate from cost and wall-clock waiting. */
export function workingTime(amount: OperatorCostAmount): string | null {
	const ran = Object.values(amount.execution.turns).some((count) => count > 0)
		|| amount.execution.seconds > 0;
	return ran
		? lowerBound(duration(amount.execution.seconds), amount.execution.unknown_turns > 0)
		: null;
}

/** Wall-clock waiting, separate from both execution time and spend. */
export function waitingTime(amount: OperatorCostAmount): string | null {
	return amount.waiting.seconds > 0 ? duration(amount.waiting.seconds) : null;
}

/** Whether sub-steps add a known fact this human summary renders. */
export function ownDiffers(node: OperatorNode): boolean {
	const { own, rolled } = node.node_cost;
	return own.tokens.known !== rolled.tokens.known
		|| own.model_cost_usd.known !== rolled.model_cost_usd.known
		|| own.execution.seconds !== rolled.execution.seconds
		|| own.waiting.seconds !== rolled.waiting.seconds;
}

/**
 * The node with its revisions and follow-ups, when they add something. A task
 * with none is its own commitment, so that total would only repeat its cost.
 */
export function withFollowUps(node: OperatorNode): string | null {
	const commitment = node.commitment_cost;
	if (!commitment) return null;
	const { rolled } = node.node_cost;
	const incomplete = commitment.tokens.unknown_rows > 0
		|| commitment.model_cost_usd.unknown_rows > 0
		|| commitment.usage_recording_failures > 0;
	const hasLaterAttempts = commitment.attempts.total > node.execution.attempt_count;
	const adds = commitment.tokens.known !== rolled.tokens.known
		|| commitment.model_cost_usd.known !== rolled.model_cost_usd.known
		|| (hasLaterAttempts && incomplete);
	if (!adds) return null;
	const parts: string[] = [];
	if (commitment.model_cost_usd.known !== null && commitment.model_cost_usd.known > 0) {
		parts.push(lowerBound(
			formatCost(commitment.model_cost_usd.known),
			commitment.model_cost_usd.unknown_rows > 0 || commitment.usage_recording_failures > 0,
		));
	}
	if (commitment.tokens.known !== null) {
		parts.push(lowerBound(
			tokens(commitment.tokens.known),
			commitment.tokens.unknown_rows > 0 || commitment.usage_recording_failures > 0,
		));
	}
	// Exact counts can differ by a few tokens and still read the same; say it once.
	const line = parts.join(' · ');
	return line && line !== costLine(rolled) ? line : null;
}

/**
 * The Initiative total line as a label and segments: the label rides with the
 * first segment, and the line wraps only between segments. Tokens, time, then the follow-up count;
 * no dollars. Each fragment is the amount's own wording, so "at least" and
 * a missing figure follow the same rules as the lines above it.
 */
export function initiativeLine(node: OperatorNode): { label: string; segments: string[] } | null {
	const initiative = node.initiative;
	if (!initiative) return null;
	const { cost, follow_ups: count, open } = initiative;
	const incomplete = cost.tokens.unknown_rows > 0 || cost.usage_recording_failures > 0;
	const segments: string[] = [];
	if (cost.tokens.known !== null) segments.push(lowerBound(tokens(cost.tokens.known), incomplete));
	else if (incomplete) segments.push('tokens not recorded');
	const working = workingTime(cost);
	if (working) segments.push(`${working} working`);
	segments.push(`${count} ${count === 1 ? 'follow-up' : 'follow-ups'}`);
	return { label: `Initiative total${open ? ' so far' : ''}:`, segments };
}

export function isFinished(node: OperatorNode): boolean {
	return node.execution.condition === 'terminal';
}

export function artifactCount(count: number): string {
	return `${count} ${count === 1 ? 'artifact' : 'artifacts'} linked`;
}
