/** The client renderer for server-owned display keys; it translates contract-v2 keys to copy without recomputing lifecycle, admission, ownership, current/next, or rollup state. */
import type {
	OperatorLiveness,
	OperatorNode,
	OperatorNodeLabelKey,
	OperatorPlanEntry,
	OperatorPlanLabelKey,
	OperatorToneKey,
	ThreadAttentionKind,
	ThreadPlanState,
	ThreadPlanStateEntry,
} from '$lib/api/plans';
import { formatMoment, parseWsTimestamp } from '$lib/time';
import { timedWait, waitCountdown, waitEndsAtMs } from './planWaits';

/** An ISO 8601 instant inside plan prose ("Waiting until 2026-09-25T12:00:00+00:00"). */
const INSTANT = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?/g;

/** "Waiting until" / "Until" in front of an instant: the row already says it waits, so only the time is left. */
const UNTIL_INSTANT = /(?:Waiting )?[Uu]ntil (?=\d{4}-\d{2}-\d{2}T)/g;

/** Plan prose with its instants localized for the viewer and redundant "until" removed. */
export function localizeInstants(text: string, now: Date = new Date()): string {
	return text.replace(UNTIL_INSTANT, '').replace(INSTANT, (instant) => {
		const ms = parseWsTimestamp(instant);
		return Number.isFinite(ms) ? formatMoment(new Date(ms), now, { zone: true }) : instant;
	});
}

export type PlanCardState = 'working' | 'needs-you' | 'waiting' | 'finished' | 'unknown';

export type StateTone = OperatorToneKey | 'unknown';
export type NodeDisplayState = PlanCardState;

const PLAN_STATES: Record<OperatorPlanLabelKey, PlanCardState> = {
	working: 'working',
	needs_you: 'needs-you',
	waiting: 'waiting',
	finished: 'finished',
};

const PLAN_LABELS: Record<PlanCardState, string> = {
	working: 'Working',
	'needs-you': 'Needs you',
	waiting: 'Waiting',
	finished: 'Finished',
	unknown: 'Unavailable',
};

const NODE_LABELS: Record<OperatorNodeLabelKey, string> = {
	working: 'Working',
	needs_you: 'Needs you',
	needs_agent_input: 'Needs agent · input',
	waiting: 'Waiting',
	finished: 'Finished',
};

/** A settled node's outcome is more useful than the server's generic `finished` key. */
const FINISHED_NODE_LABELS: Partial<Record<OperatorNode['state'], string>> = {
	accepted: 'Done',
	accepted_with_followup: 'Done',
	closed: 'Won’t do',
	failed: 'Failed',
};

/** An open container the server rolls up as finished reads the word of its work. */
const FINISHED_BRANCH_LABELS: Record<string, string> = {
	complete: 'Done',
	closed: 'Won’t do',
	failed: 'Failed',
};

const NODE_STATES: Record<OperatorNodeLabelKey, NodeDisplayState> = {
	working: 'working',
	needs_you: 'needs-you',
	needs_agent_input: 'waiting',
	waiting: 'waiting',
	finished: 'finished',
};

/** Read the plan state selected by a server-owned display key. */
export function planCardState(entry: { display: { label_key: string } }): PlanCardState {
	const key = entry.display.label_key;
	return Object.hasOwn(PLAN_STATES, key)
		? PLAN_STATES[key as OperatorPlanLabelKey]
		: 'unknown';
}

/** Read the node state selected by the server projection. */
export function nodeDisplayState(node: OperatorNode): NodeDisplayState {
	const key = node.display.label_key as string;
	return Object.hasOwn(NODE_STATES, key)
		? NODE_STATES[key as OperatorNodeLabelKey]
		: 'unknown';
}

/** Translate one server-owned node key to user-facing copy. */
export function nodeDisplayLabel(node: OperatorNode, _isNext = false): string {
	const key = node.display.label_key as string;
	if (key === 'finished') {
		const branch = node.branch_progress ? FINISHED_BRANCH_LABELS[node.display.tone_key] : undefined;
		return FINISHED_NODE_LABELS[node.state] ?? branch ?? NODE_LABELS.finished;
	}
	return Object.hasOwn(NODE_LABELS, key)
		? NODE_LABELS[key as OperatorNodeLabelKey]
		: 'Unavailable';
}

/** A row’s secondary copy: its server cause, failed-work attempt, or requested user input. */
const SETTLED_TONES: ReadonlySet<StateTone> = new Set(['complete', 'closed', 'failed']);

/** When a settled row settled, in local time; null for open work. */
export function settledMoment(node: OperatorNode, now: Date = new Date()): string | null {
	if (!SETTLED_TONES.has(nodeStateTone(node))) return null;
	const ms = parseWsTimestamp(node.last_transition_at);
	return Number.isFinite(ms) ? formatMoment(new Date(ms), now) || null : null;
}

/** When a timed wait ends, as a person says it: "tomorrow at 9:00 AM", or "ends in 12 min" inside the hour. */
function endMoment(node: OperatorNode, now: Date, clock = false): string | null {
	const ends = waitEndsAtMs(node);
	if (ends === null) return null;
	return waitCountdown(node, now) ?? formatMoment(new Date(ends), now, { clock });
}

/**
 * A task its agent scheduled, said as a person reads it: "tomorrow at 9:00 AM ·
 * why", or "ends in 12 min · why" inside the hour, from the wait's own
 * deadline, never the instant in the prose. Work that is running says only why,
 * because a running task is not waiting for the time.
 */
function scheduledLine(node: OperatorNode, now: Date, running: boolean, clock = false): string | null {
	const why = timedWait(node)?.scheduled_reason;
	const when = endMoment(node, now, clock);
	if (!why || when === null) return null;
	return running ? why : `${when} · ${why}`;
}

function finishedOutcome(node: OperatorNode): string {
	const label = nodeDisplayLabel(node);
	switch (node.state) {
		case 'accepted': return node.status_reason ? `${label} · ${node.status_reason}` : label;
		case 'accepted_with_followup':
			return node.status_reason
				? `${label} · follow-up created · ${node.status_reason}`
				: `${label} · follow-up created`;
		case 'failed': return label;
		case 'closed': return node.status_reason ? `${label} · ${node.status_reason}` : label;
		// An open container whose work all finished says what its work did; a
		// leaf archived before it settled is the only other open `finished`.
		default: return node.branch_progress ? label : 'Archived';
	}
}

function openReason(node: OperatorNode, now: Date, view?: ScheduledView): string | null {
	const running = node.display.label_key === 'working';
	const scheduled = scheduledLine(node, now, running || view === 'reason', view === 'clock');
	if (scheduled) return scheduled;
	const countdown = running ? null : waitCountdown(node, now);
	const parts: string[] = [];
	if (countdown) parts.push(countdown);
	// A countdown already says when a plain deadline ends; don't say it twice.
	const saysDeadline = node.status_reason?.startsWith('Waiting until ')
		&& (running || (countdown !== null && node.status_reason === timedWait(node)?.reason));
	if (node.status_reason && !saysDeadline) parts.push(localizeInstants(node.status_reason, now));
	if (!parts.length && node.review) parts.push(node.review.label);
	if (!parts.length && node.requires_user_action) {
		const ask = node.artifacts.find((artifact) => artifact.role === 'attention');
		parts.push(ask?.title ?? 'Assigned to you');
	}
	return parts.length ? parts.join(' · ') : null;
}

/** Scheduled-row time: `clock` keeps a far-date clock; `reason` leaves its cluster line to name the moment. */
export type ScheduledView = 'clock' | 'reason';

export function nodeSubline(node: OperatorNode, now: Date = new Date(), view?: ScheduledView): string | null {
	const { lead, moment } = nodeSublineParts(node, now, view);
	return lead && moment ? `${lead} · ${moment}` : lead ?? moment;
}

/** The narrow-row split: `lead` yields first while a settled `moment` stays whole. */
export function nodeSublineParts(
	node: OperatorNode,
	now: Date = new Date(),
	view?: ScheduledView,
): { lead: string | null; moment: string | null } {
	const parts: string[] = [];
	if (node.display.label_key === 'finished') {
		parts.push(finishedOutcome(node));
	} else {
		// The round or attempt leads: a long reason is what a narrow row cuts.
		// friction_count includes the send-back round; one send-back is ordinary review.
		const round = node.execution.revision_round ?? 0;
		if (round >= 2) parts.push(`Round ${round}`);
		else if (node.execution.friction_count - round >= 1) {
			parts.push(`Attempt ${node.execution.friction_count + 1}`);
		}
		const reason = openReason(node, now, view);
		if (reason) parts.push(reason);
	}
	// A row is one line: a reason's line breaks (send-back feedback, a pasted list) read as spaces.
	const lead = parts.join(' · ').replace(/\s+/g, ' ').trim();
	return { lead: lead || null, moment: settledMoment(node, now) };
}

export function cardStateLabel(state: PlanCardState): string {
	return PLAN_LABELS[state];
}

/** The server owns tone; every client surface renders it unchanged. */
export function cardStateTone(entry: { display: { tone_key: string } }): StateTone {
	const key = entry.display.tone_key as string;
	return Object.hasOwn(TONE_LABELS, key) ? key as OperatorToneKey : 'unknown';
}

const TONE_LABELS: Record<OperatorToneKey, true> = {
	active: true,
	'human-review': true,
	waiting: true,
	complete: true,
	failed: true,
	closed: true,
};



/** The server owns tone; every node surface renders it unchanged. */
export function nodeStateTone(node: OperatorNode): StateTone {
	const key = node.display.tone_key as string;
	return Object.hasOwn(TONE_LABELS, key) ? key as OperatorToneKey : 'unknown';
}

/** Child summaries expose canonical liveness but not loaded node detail. */
export function livenessTone(liveness: OperatorLiveness): StateTone {
	switch (liveness) {
		case 'advancing': return 'active';
		case 'needs-review': return 'human-review';
		case 'blocked':
		case 'waiting':
		case 'paused':
		case 'idle': return 'waiting';
		default: return 'unknown';
	}
}

export function livenessWord(liveness: OperatorLiveness): string {
	switch (liveness) {
		case 'advancing': return 'active';
		case 'blocked': return 'blocked';
		case 'needs-review': return 'awaiting review';
		case 'waiting': return 'waiting';
		case 'paused': return 'paused';
		case 'idle': return 'idle';
		default: return 'unavailable';
	}
}

const CHAT_WORDS: Record<ThreadPlanState, string> = {
	moving: 'Moving',
	needs_input: 'Needs input',
	blocked_on_you: 'Blocked on you',
	failed: 'Failed',
	stuck: 'Stuck',
	paused: 'Paused',
	idle: 'Idle',
};

export function chatStateWord(state: ThreadPlanState): string {
	return Object.hasOwn(CHAT_WORDS, state) ? CHAT_WORDS[state] : 'Idle';
}

const ATTENTION_KIND_WORDS: Record<ThreadAttentionKind, string> = {
	decision: 'Decide',
	review: 'Review',
	access: 'Allow access',
	hold: 'Needs you',
	failed: 'Failed',
};

/** One item's own word, the same vocabulary the row mark uses per kind. */
export function attentionKindWord(kind: ThreadAttentionKind): string {
	return Object.hasOwn(ATTENTION_KIND_WORDS, kind) ? ATTENTION_KIND_WORDS[kind] : kind;
}

/** A chat row's words for its thread, plus the plan header's label and a pause when they add detail. */
export function chatStateTitle(entry: ThreadPlanStateEntry): string {
	const word = chatStateWord(entry.state);
	const key = entry.label_key ?? '';
	const header = Object.hasOwn(PLAN_STATES, key)
		? PLAN_LABELS[PLAN_STATES[key as OperatorPlanLabelKey]]
		: null;
	const parts = [word];
	if (header && header !== word) parts.push(`Plan: ${header}`);
	if (entry.paused && entry.state !== 'paused') parts.push('Paused');
	return parts.join(' · ');
}
