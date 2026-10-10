import { writable } from 'svelte/store';

export type CheckpointTrigger = 'manual' | 'pressure' | 'model_switch' | 'idle' | 'agent';
export type CheckpointMode = 'warm' | 'cold';
export type CheckpointStatus =
	| 'running'
	| 'captured'
	| 'installed'
	| 'overtaken'
	| 'failed'
	| 'cancelled';

export interface Checkpoint {
	id: string;
	trigger: CheckpointTrigger;
	mode: CheckpointMode;
	status: CheckpointStatus;
	writerModel: string;
	error: string | null;
	startedAt: string;
}

export interface CheckpointState {
	checkpoints: readonly Checkpoint[];
	loading: boolean;
	error: string | null;
}

const EMPTY_STATE: CheckpointState = Object.freeze({
	checkpoints: [],
	loading: false,
	error: null,
});

export const checkpointsByThread = writable<ReadonlyMap<string, CheckpointState>>(
	new Map(),
);

function updateState(
	threadId: string,
	update: (state: CheckpointState) => CheckpointState,
): void {
	checkpointsByThread.update(states => {
		const next = new Map(states);
		next.set(threadId, update(states.get(threadId) ?? EMPTY_STATE));
		return next;
	});
}

function checkpointRows(body: unknown): readonly Checkpoint[] {
	if (!body || typeof body !== 'object') {
		throw new Error('Checkpoint history response is unavailable');
	}
	const rows = (body as { checkpoints?: unknown }).checkpoints;
	if (!Array.isArray(rows)) {
		throw new Error('Checkpoint history response is unavailable');
	}
	return rows as Checkpoint[];
}

function checkpointRow(body: unknown): Checkpoint {
	if (!body || typeof body !== 'object') {
		throw new Error('Checkpoint action response is unavailable');
	}
	const checkpoint = (body as { checkpoint?: unknown }).checkpoint;
	if (!checkpoint || typeof checkpoint !== 'object') {
		throw new Error('Checkpoint action response is unavailable');
	}
	return checkpoint as Checkpoint;
}

function checkpointUrl(threadId: string, token: string): string {
	return `/api/threads/${encodeURIComponent(threadId)}/checkpoints`
		+ `?token=${encodeURIComponent(token)}`;
}

function upsertCheckpoint(threadId: string, checkpoint: Checkpoint): void {
	updateState(threadId, state => ({
		checkpoints: [
			checkpoint,
			...state.checkpoints.filter(row => row.id !== checkpoint.id),
		],
		loading: false,
		error: null,
	}));
}

export function activeCheckpoint(state: CheckpointState | undefined): Checkpoint | null {
	return state?.checkpoints.find(checkpoint =>
		checkpoint.status === 'running' || checkpoint.status === 'captured'
	) ?? null;
}

export function latestCheckpoint(state: CheckpointState | undefined): Checkpoint | null {
	return state?.checkpoints[0] ?? null;
}

/**
 * Checkpoints are automatic; Checkpoint Now is only an optional shortcut, so
 * when the backend says one is not currently possible the sheet shows how
 * fresh the last one is instead of a disabled button (#1952). No turn count
 * is tracked per checkpoint, so this is minutes/hours since `startedAt`.
 */
export function checkpointFreshnessLabel(checkpoint: Checkpoint, now = Date.now()): string {
	const minutes = Math.max(0, Math.round((now - new Date(checkpoint.startedAt).getTime()) / 60_000));
	if (minutes < 1) return 'Checkpointed just now';
	if (minutes < 60) return `Checkpointed ${minutes} minute${minutes === 1 ? '' : 's'} ago`;
	const hours = Math.round(minutes / 60);
	return `Checkpointed ${hours} hour${hours === 1 ? '' : 's'} ago`;
}

export function checkpointStatusCopy(checkpoint: Checkpoint): string {
	switch (checkpoint.status) {
		case 'running':
			return checkpoint.trigger === 'model_switch'
				? 'Preparing model switch…'
				: 'Saving context…';
		case 'captured': return 'Finishing checkpoint…';
		case 'installed': return 'Context saved';
		case 'overtaken': return 'Checkpoint saved to history';
		case 'failed': return 'Checkpoint failed';
		case 'cancelled': return 'Checkpoint cancelled';
	}
}

export async function loadCheckpoints(
	threadId: string,
	token: string,
): Promise<readonly Checkpoint[]> {
	updateState(threadId, state => ({ ...state, loading: true, error: null }));
	try {
		const response = await fetch(checkpointUrl(threadId, token));
		if (!response.ok) throw new Error(`Checkpoint refresh failed (${response.status})`);
		const checkpoints = checkpointRows(await response.json());
		updateState(threadId, () => ({ checkpoints, loading: false, error: null }));
		return checkpoints;
	} catch (error) {
		const message = error instanceof Error ? error.message : 'Checkpoint status unavailable';
		updateState(threadId, state => ({ ...state, loading: false, error: message }));
		throw error;
	}
}

export function invalidateCheckpoints(
	threadId: string,
	token: string,
): Promise<readonly Checkpoint[]> {
	return loadCheckpoints(threadId, token);
}

/** A checkpoint request the backend refused, with its exact reason (#1952). */
export class CheckpointRefusalError extends Error {
	readonly reason: string;

	constructor(message: string, reason: string) {
		super(message);
		this.name = 'CheckpointRefusalError';
		this.reason = reason;
	}
}

async function parseCheckpointRefusal(
	response: Response,
	fallback: string,
): Promise<CheckpointRefusalError> {
	try {
		const body = await response.json();
		const detail = (body as { detail?: unknown } | null)?.detail;
		if (detail && typeof detail === 'object') {
			const reason = (detail as { reason?: unknown }).reason;
			const message = (detail as { message?: unknown }).message;
			if (reason === 'no_new_history') {
				return new CheckpointRefusalError(
					'Nothing new since the last checkpoint.',
					'no_new_history',
				);
			}
			if (typeof message === 'string' && message) {
				return new CheckpointRefusalError(message, 'unknown');
			}
		}
		if (typeof detail === 'string' && detail) {
			return new CheckpointRefusalError(detail, 'unknown');
		}
	} catch {
		// Non-JSON or empty body; fall through to the generic message below.
	}
	return new CheckpointRefusalError(fallback, 'unknown');
}

async function checkpointAction(
	threadId: string,
	token: string,
	url: string,
	userMessage: string,
): Promise<Checkpoint> {
	const response = await fetch(url, { method: 'POST' });
	if (!response.ok) throw await parseCheckpointRefusal(response, userMessage);
	const checkpoint = checkpointRow(await response.json());
	upsertCheckpoint(threadId, checkpoint);
	return checkpoint;
}

export function requestManualCheckpoint(
	threadId: string,
	token: string,
): Promise<Checkpoint> {
	return checkpointAction(
		threadId,
		token,
		checkpointUrl(threadId, token),
		'Could not start a checkpoint. Try again.',
	);
}

export function cancelCheckpoint(
	threadId: string,
	checkpointId: string,
	token: string,
): Promise<Checkpoint> {
	const url = `/api/threads/${encodeURIComponent(threadId)}/checkpoints/`
		+ `${encodeURIComponent(checkpointId)}/cancel?token=${encodeURIComponent(token)}`;
	return checkpointAction(
		threadId,
		token,
		url,
		'Could not cancel the checkpoint. Try again.',
	);
}

export function _resetCheckpointsForTests(): void {
	checkpointsByThread.set(new Map());
}
