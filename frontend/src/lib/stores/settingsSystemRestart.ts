import { get } from 'svelte/store';
import type { UpdatePhase } from '../update/model';
import { token } from './auth';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';

const BASE = '';

export async function restartSystem(
	mode: 'restart' | 'update' = 'restart',
): Promise<{ ok: true; operation_id: string; message?: string } | { ok: false; message?: string }> {
	try {
		const t = get(token);
		const res = await fetch(`${BASE}/api/system/restart?token=${t}&mode=${mode}`, {
			method: 'POST',
		});
		const data = await res.json().catch(() => ({}));
		if (!res.ok) {
			if (data?.detail !== undefined) console.error('[settings] restart rejected', data.detail);
			return {
				ok: false,
				message: plainErrorText(data?.detail, 'The update failed before restarting. Nothing was changed.'),
			};
		}
		if (!data?.operation_id) return { ok: false, message: 'Restart was not accepted. No durable operation was created.' };
		return { ok: true, operation_id: data.operation_id, message: data.message };
	} catch (e) {
		console.error('[settings] restart request failed', e);
		return { ok: false, message: SERVER_UNREACHABLE };
	}
}

export interface SystemVersionSnapshot {
	frontend?: { full_hash?: string | null };
	backend?: {
		full_hash?: string | null;
		start_iso?: string | null;
		current_head?: { full_hash?: string | null };
	};
}

export async function readSystemVersion(signal?: AbortSignal): Promise<SystemVersionSnapshot | null> {
	try {
		const res = await fetch('/api/version', { cache: 'no-store', signal });
		if (!res.ok) return null;
		return await res.json();
	} catch {
		return null;
	}
}

export interface SystemRestartStatus {
	status: 'in_flight' | 'complete' | 'failed' | 'cancelled' | 'superseded';
	phase?: UpdatePhase | null;
	error?: string | null;
}

type RestartStatusRead = SystemRestartStatus | string | null;

export async function readSystemRestartStatus(operationId: string, signal?: AbortSignal): Promise<RestartStatusRead> {
	try {
		const t = get(token);
		if (!t) return null;
		const res = await fetch(
			`${BASE}/api/system/drain?token=${encodeURIComponent(t)}&operation_id=${encodeURIComponent(operationId)}`,
			{ cache: 'no-store', signal },
		);
		if (res.status === 404) return 'Restart operation not found.';
		if (!res.ok) return null;
		return await res.json();
	} catch {
		return null;
	}
}

// Restart observation remains active until the durable operation settles.

type RestartObservation = {
	version: SystemVersionSnapshot | null;
	restartRead: RestartStatusRead;
};

type RestartObservationResult = {
	phase?: string;
	result?: { ok: boolean; message?: string };
	sawUnavailable: boolean;
	drainEndedAt: number | null;
};

function restartPhase(restartStatus: SystemRestartStatus | null, hasVersion: boolean): string {
	if (restartStatus?.status === 'in_flight') {
		if (restartStatus.phase === 'restarting') return 'Restarting backend…';
		if (restartStatus.phase === 'resuming') return 'Restoring restart continuity…';
		if (restartStatus.phase === 'verifying') return 'Checking the new version…';
		if (restartStatus.phase === 'ready') return 'Drain complete. Starting replacement…';
		return 'Draining active work…';
	}
	if (!hasVersion) return 'Restarting backend…';
	return restartStatus ? 'Checking restart result…' : 'Waiting for drain to begin…';
}

async function observeSystemRestart(operationId: string, signal?: AbortSignal): Promise<RestartObservation> {
	const [version, restartRead] = await Promise.all([
		readSystemVersion(signal),
		readSystemRestartStatus(operationId, signal),
	]);
	return { version, restartRead };
}

function classifyRestartObservation(
	before: SystemVersionSnapshot | null,
	observation: RestartObservation,
	sawUnavailable: boolean,
	drainEndedAt: number | null,
	inferStoppedReplacement = true,
): RestartObservationResult {
	const { version, restartRead } = observation;
	if (typeof restartRead === 'string') {
		return { result: { ok: false, message: restartRead }, sawUnavailable, drainEndedAt };
	}
	if (['failed', 'cancelled', 'superseded'].includes(restartRead?.status ?? '')) {
		return {
			result: { ok: false, message: restartFailureMessage(restartRead) },
			sawUnavailable,
			drainEndedAt,
		};
	}

	const backend = version?.backend;
	const restarted = Boolean(
		backend?.start_iso && (
			(before?.backend?.start_iso && backend.start_iso !== before.backend.start_iso)
			|| (!before?.backend?.start_iso && sawUnavailable)
		),
	);
	if (restartRead?.status === 'in_flight') {
		drainEndedAt = null;
	} else if (!version) {
		sawUnavailable = true;
	} else if (restartRead) {
		drainEndedAt ??= Date.now();
	}

	const phase = restartPhase(restartRead, Boolean(version));
	if (restarted && restartRead?.status === 'complete') {
		return { phase, result: { ok: true }, sawUnavailable, drainEndedAt };
	}
	if (
		inferStoppedReplacement
		&& drainEndedAt !== null
		&& !sawUnavailable
		&& restartRead?.status !== 'complete'
		&& Date.now() - drainEndedAt >= 2_000
	) {
		return {
			phase,
			result: { ok: false, message: 'Restart stopped before replacing the server. Inspect the restart log before retrying.' },
			sawUnavailable,
			drainEndedAt,
		};
	}
	return { phase, sawUnavailable, drainEndedAt };
}

async function observeSystemRestartWithin(
	operationId: string,
	timeoutMs: number,
): Promise<RestartObservation | null> {
	const controller = new AbortController();
	let timer: ReturnType<typeof setTimeout> | null = null;
	try {
		const timeout = new Promise<null>((resolve) => {
			timer = setTimeout(() => {
				controller.abort();
				resolve(null);
			}, timeoutMs);
		});
		return await Promise.race([
			observeSystemRestart(operationId, controller.signal),
			timeout,
		]);
	} finally {
		if (timer !== null) clearTimeout(timer);
	}
}

/** Update failure copy lives in `lib/update/model.ts`; a plain restart shows its stored summary. */
function restartFailureMessage(status: SystemRestartStatus | null | undefined): string {
	return status?.error ?? 'Restart operation ended before a replacement was verified.';
}

export async function waitForSystemRestart(
	before: SystemVersionSnapshot | null,
	operationId: string,
	onPhase?: (message: string) => void,
): Promise<{ ok: boolean; message?: string }> {
	let sawUnavailable = false;
	let drainEndedAt: number | null = null;
	while (true) {
		const observation = await observeSystemRestartWithin(operationId, 5_000);
		if (observation === null) {
			sawUnavailable = true;
			onPhase?.('Restarting backend…');
			await new Promise((resolve) => setTimeout(resolve, 500));
			continue;
		}
		const outcome = classifyRestartObservation(before, observation, sawUnavailable, drainEndedAt);
		sawUnavailable = outcome.sawUnavailable;
		drainEndedAt = outcome.drainEndedAt;
		if (outcome.phase) onPhase?.(outcome.phase);
		if (outcome.result) {
			if (outcome.result.ok) onPhase?.('Restart complete. Reloading…');
			return outcome.result;
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
}
