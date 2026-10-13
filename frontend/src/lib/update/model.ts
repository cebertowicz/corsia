/**
 * The Update journey's data model and its pure derivations: what the backend
 * reports about an update operation, and the step, copy and numbers every
 * surface (toast, sheet, Settings, composer) shows from it.
 */

export type UpdatePhase = 'holding' | 'steering' | 'draining' | 'ready' | 'restarting' | 'resuming';
export type UpdateOperationStatus = 'in_flight' | 'complete' | 'failed' | 'cancelled' | 'superseded';
export type UpdateFailureCode =
	| 'disk_space'
	| 'fetch_failed'
	| 'build_failed'
	| 'launch_failed'
	| 'drain_cancelled';

export interface UpdateOperation {
	operation_id: string;
	kind?: 'update' | 'restart' | null;
	trigger?: string | null;
	status: UpdateOperationStatus;
	phase?: UpdatePhase | null;
	target_version?: string | null;
	drain_total?: number | null;
	blocking?: number | null;
	hard_cap_remaining?: number | null;
	failure_code?: UpdateFailureCode | null;
	failure_detail?: string | null;
	conductor_told_at?: string | null;
	error?: string | null;
	requested_at?: string | null;
	settled_at?: string | null;
	log_tail?: string | null;
}

export interface UpdateChanges {
	kind: 'changelog' | 'commits';
	headline: string | null;
	items: string[];
	more: number;
}

/** What the sheet and Settings need to know about an update that is on offer. */
export interface UpdateOffer {
	version: string;
	currentVersion: string | null;
	changes: UpdateChanges | null;
}

/** A failure to show: a stored operation, or a verdict the client reached itself. */
export interface UpdateFailure {
	code: UpdateFailureCode | null;
	detail: string | null;
	summary: string | null;
	conductorTold: boolean;
	/** Set when the client, not the operation, found the problem. */
	clientMessage?: string;
	log: string | null;
}

export const UPDATE_STEPS = [
	'Getting the update',
	'Stopping agents safely',
	'Restarting CORSIA',
	'Checking everything and resuming agents',
] as const;

export function isInFlight(op: UpdateOperation | null | undefined): op is UpdateOperation & { status: 'in_flight' } {
	return op?.status === 'in_flight';
}

/** The backend refuses new work once an update has left `holding`. */
export function holdsNewWork(op: UpdateOperation | null | undefined): boolean {
	return isInFlight(op) && op.kind === 'update' && op.phase !== 'holding';
}

/** The step the operation is on, 0 to 3. A server that cannot be reached is restarting. */
export function stepIndex(op: UpdateOperation | null, unreachable: boolean): number {
	if (unreachable && op && op.phase !== 'holding') return 2;
	switch (op?.phase) {
		case 'steering':
		case 'draining':
			return 1;
		case 'ready':
		case 'restarting':
			return 2;
		case 'resuming':
			return 3;
		default:
			return 0;
	}
}

export function formatElapsed(totalSeconds: number): string {
	const seconds = Math.max(0, Math.floor(totalSeconds));
	const minutes = Math.floor(seconds / 60);
	return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

/** "today at 11:42 AM" for a same-day timestamp, otherwise "Oct 9, 4:15 PM". */
export function formatInstalledAt(iso: string, nowMs: number): string {
	const at = new Date(iso);
	if (Number.isNaN(at.getTime())) return '';
	const time = at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
	if (at.toDateString() === new Date(nowMs).toDateString()) return `today at ${time}`;
	return `${at.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

/** Seconds since the operation was requested, on the server's clock. */
export function elapsedSeconds(op: UpdateOperation | null, nowMs: number, serverOffsetMs: number): number {
	const started = op?.requested_at ? Date.parse(op.requested_at) : Number.NaN;
	if (Number.isNaN(started)) return 0;
	return Math.max(0, (nowMs + serverOffsetMs - started) / 1000);
}

/** "a minute", "2 minutes", "30 seconds": the way a person says a duration. */
export function humanDuration(seconds: number): string {
	if (seconds < 90) return seconds < 45 ? `${Math.max(1, Math.round(seconds))} seconds` : 'a minute';
	const minutes = Math.round(seconds / 60);
	return `${minutes} minutes`;
}

export function agentsSafe(op: UpdateOperation | null): { safe: number; total: number } | null {
	const total = op?.drain_total ?? 0;
	if (!op || total <= 0) return null;
	const waiting = Math.min(total, op.blocking ?? total);
	return { safe: total - waiting, total };
}

function agentsLabel(count: number): string {
	return `${count} agent${count === 1 ? '' : 's'}`;
}

/** The one line the progress toast carries, without its ticking time. */
export function stepMessage(op: UpdateOperation | null, unreachable: boolean): string {
	const step = stepIndex(op, unreachable);
	if (step === 0) return 'Getting the update';
	if (step === 1) {
		const counts = agentsSafe(op);
		return counts ? `Stopping agents · ${counts.safe} of ${counts.total} safe` : 'Stopping agents';
	}
	if (step === 2) return 'Restarting';
	const total = op?.drain_total ?? 0;
	return total > 0 ? `Resuming ${agentsLabel(total)}` : 'Resuming';
}

/** The sentence under the active step in the panel. */
export function stepDetail(op: UpdateOperation | null, unreachable: boolean): string | null {
	if (stepIndex(op, unreachable) !== 1) return null;
	const counts = agentsSafe(op);
	if (!counts) return null;
	const base = `${counts.safe} of ${counts.total} agents are at a safe point.`;
	const remaining = op?.hard_cap_remaining;
	if (counts.safe === counts.total || remaining == null) return base;
	return `${base} Stopping the rest in ${formatElapsed(remaining)}.`;
}

function parseDetail(detail: string | null | undefined): Record<string, unknown> {
	try {
		const parsed = JSON.parse(detail ?? '{}');
		return parsed && typeof parsed === 'object' ? parsed : {};
	} catch {
		return {};
	}
}

export function diskNumbers(detail: string | null | undefined): { neededGb: number; freeGb: number } | null {
	const parsed = parseDetail(detail);
	const needed = Number(parsed.needed_bytes);
	const free = Number(parsed.free_bytes);
	if (!Number.isFinite(needed) || !Number.isFinite(free) || needed <= 0) return null;
	return { neededGb: Math.ceil(needed / 1e9), freeGb: Math.max(0, Math.floor(free / 1e9)) };
}

export function failureFromOperation(op: UpdateOperation): UpdateFailure {
	return {
		code: op.failure_code ?? null,
		detail: op.failure_detail ?? null,
		summary: op.error ?? null,
		conductorTold: Boolean(op.conductor_told_at),
		log: op.log_tail ?? null,
	};
}

export interface FailureCopy {
	/** The panel's header and the toast's first line. */
	title: string;
	/** The one plain sentence naming the cause. */
	headline: string;
	/** What state CORSIA is in, with the figures a refusal carries. */
	body: string;
	/** The toast's second line: the number or fact to act on. */
	toastLine: string | null;
	/** The retry's label, named for the fix. */
	retryLabel: string | null;
}

const RAN_ON = 'CORSIA kept running and nothing was changed.';

function diskCopy(detail: string | null): FailureCopy {
	const disk = diskNumbers(detail);
	const body = disk ? `Needs ${disk.neededGb} GB free; this Mac has ${disk.freeGb} GB. ${RAN_ON}` : RAN_ON;
	return {
		title: "Update didn't start",
		headline: 'Not enough disk space for a safety backup.',
		body,
		toastLine: disk ? `Needs ${Math.max(1, disk.neededGb - disk.freeGb)} GB more free space` : null,
		retryLabel: 'Try again after freeing space',
	};
}

/** One plain sentence per failure code; the stored summary when there is no code. */
export function failureCopy(failure: UpdateFailure): FailureCopy {
	if (failure.clientMessage) {
		return {
			title: "The update didn't finish",
			headline: 'The update finished, but the running versions do not match.',
			body: failure.clientMessage,
			toastLine: null,
			retryLabel: null,
		};
	}
	switch (failure.code) {
		case 'disk_space':
			return diskCopy(failure.detail);
		case 'fetch_failed':
			return {
				title: "Update didn't start",
				headline: "Couldn't download the update.",
				body: RAN_ON,
				toastLine: null,
				retryLabel: 'Try again',
			};
		case 'build_failed':
			return {
				title: "Update didn't start",
				headline: "Couldn't build the update.",
				body: RAN_ON,
				toastLine: null,
				retryLabel: 'Try again',
			};
		case 'launch_failed':
			return {
				title: "Update didn't finish",
				headline: "Couldn't start the restart.",
				body: 'CORSIA is still running.',
				toastLine: null,
				retryLabel: 'Try again',
			};
		case 'drain_cancelled':
			return {
				title: 'Update cancelled',
				headline: 'The update was cancelled before CORSIA restarted.',
				body: RAN_ON,
				toastLine: null,
				retryLabel: 'Try again',
			};
		default:
			return {
				title: "The update didn't finish",
				headline: failure.summary ?? 'The update ended before CORSIA could confirm it.',
				body: '',
				toastLine: null,
				retryLabel: 'Try again',
			};
	}
}

/** Failure copy for an operation that ended without the update installing. */
export function isFailedOperation(op: UpdateOperation | null | undefined): op is UpdateOperation {
	return op?.status === 'failed' || op?.status === 'cancelled';
}
