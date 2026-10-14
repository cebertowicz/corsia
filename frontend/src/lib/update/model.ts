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
	| 'drain_cancelled'
	| 'drain_failed'
	| 'update_timed_out'
	| 'prepare_failed'
	| 'index_locked'
	| 'checkout_detached'
	| 'checkout_dirty'
	| 'restart_pending';

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
	/** The change list this update was offered with; it lives on the row so every device shows the same notes. */
	changes?: UpdateChanges | null;
	/** Set by the first client to show the outcome (a failure only once someone acts on it). */
	outcome_seen_at?: string | null;
	/** A staged update that stopped awaiting a restart, and a restart has since finished: the failure is history. */
	activated_by_restart?: boolean;
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

/** Format the configured 24-hour schedule value wherever a person sees it. */
export function formatScheduleTime(value: string | null | undefined): string {
	const matched = /^(\d{2}):(\d{2})$/.exec(value ?? '');
	if (!matched) return '';
	const hour = Number(matched[1]);
	const minute = Number(matched[2]);
	if (hour > 23 || minute > 59) return '';
	return `${hour % 12 || 12}:${String(minute).padStart(2, '0')} ${hour < 12 ? 'AM' : 'PM'}`;
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
	const base = `${counts.safe} of ${counts.total} ${counts.total === 1 ? 'agent is' : 'agents are'} at a safe point.`;
	const remaining = op?.hard_cap_remaining;
	if (counts.safe === counts.total || remaining == null) return base;
	return `${base} Stopping the rest in ${formatElapsed(remaining)}.`;
}

/** A problem the client itself found, which no stored operation holds. */
export interface ClientVerdict {
	/** `version_mismatch`: a completed update whose running versions disagree; `refused`: a start the server turned away. */
	kind: 'version_mismatch' | 'refused';
	message: string;
}

/** A failure to show: a stored operation, or a verdict the client reached itself. */
export interface UpdateFailure {
	code: UpdateFailureCode | null;
	detail: string | null;
	summary: string | null;
	conductorTold: boolean;
	/** Set when the client, not the operation, found the problem; it wins over the operation's own copy. */
	client?: ClientVerdict;
	log: string | null;
}

/** An operation that ended without the update installing. */
export function isFailedOperation(op: UpdateOperation | null | undefined): op is UpdateOperation {
	return op?.status === 'failed' || op?.status === 'cancelled';
}

/** What a surface needs to know about the update on offer. */
interface OfferState {
	available?: boolean | null;
	target_version?: string | null;
	changes?: UpdateChanges | null;
}

/** A failure the server reports as resolved: its staged update came up with a later restart. */
export function isResolvedFailure(op: UpdateOperation | null | undefined): boolean {
	return isFailedOperation(op) && op.activated_by_restart === true;
}

/** The one rule for which operation a surface treats as current: a settled one targeting an older version than the offer, or a failure a restart resolved, is history. */
export function currentOperation(op: UpdateOperation | null | undefined, status: OfferState | null | undefined): UpdateOperation | null {
	if (!op || isInFlight(op)) return op ?? null;
	if (isResolvedFailure(op)) return null;
	const offered = status?.available ? status.target_version : null;
	return offered && op.target_version && offered !== op.target_version ? null : op;
}

export interface UpdateReleaseNotes {
	targetVersion: string;
	changes: UpdateChanges | null;
}

/** The notes of the current operation, or of the offer when no operation is current; both come from the server. */
export function releaseNotesFor(op: UpdateOperation | null | undefined, status: OfferState | null | undefined): UpdateReleaseNotes | null {
	const targetVersion = op?.target_version ?? status?.target_version;
	return targetVersion ? { targetVersion, changes: op?.changes ?? status?.changes ?? null } : null;
}
