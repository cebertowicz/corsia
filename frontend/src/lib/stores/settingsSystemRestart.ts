import { get } from 'svelte/store';
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
	phase?: 'holding' | 'steering' | 'draining' | 'ready' | 'restarting' | 'resuming' | null;
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

// Cover the 60s drain cap, bounded launchd handoff, and 60s readiness bound.
const SYSTEM_RESTART_TIMEOUT_MS = 4 * 60_000;
const FINAL_RESTART_OBSERVATION_TIMEOUT_MS = 5_000;

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

function restartPhase(
	restartStatus: SystemRestartStatus | null,
	hasVersion: boolean,
	restarted: boolean,
	revisionsAgree: boolean,
): string {
	if (restartStatus?.status === 'in_flight') {
		if (restartStatus.phase === 'restarting') return 'Restarting backend…';
		if (restartStatus.phase === 'resuming') return 'Restoring restart continuity…';
		if (restartStatus.phase === 'ready') return 'Drain complete. Starting replacement…';
		return 'Draining active work…';
	}
	if (!hasVersion) return 'Restarting backend…';
	if (restarted && !revisionsAgree) return 'Verifying backend, frontend, and checkout revisions…';
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
	mode: 'restart' | 'update',
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
			result: { ok: false, message: restartRead?.error ?? 'Restart operation ended before a replacement was verified.' },
			sawUnavailable,
			drainEndedAt,
		};
	}

	const backend = version?.backend;
	const backendSha = backend?.full_hash;
	const frontendSha = version?.frontend?.full_hash;
	const checkoutSha = backend?.current_head?.full_hash;
	const restarted = Boolean(
		backend?.start_iso && (
			(before?.backend?.start_iso && backend.start_iso !== before.backend.start_iso)
			|| (!before?.backend?.start_iso && sawUnavailable)
		),
	);
	const revisionsAgree = Boolean(
		backendSha && backendSha !== 'unknown'
		&& frontendSha && frontendSha !== 'unknown'
		&& frontendSha === backendSha
		&& (!checkoutSha || (checkoutSha !== 'unknown' && checkoutSha === backendSha)),
	);
	if (restartRead?.status === 'in_flight') {
		drainEndedAt = null;
	} else if (!version) {
		sawUnavailable = true;
	} else if (restartRead) {
		drainEndedAt ??= Date.now();
	}

	const phase = restartPhase(restartRead, Boolean(version), restarted, revisionsAgree);
	if (restarted && revisionsAgree && restartRead?.status === 'complete') {
		return { phase, result: { ok: true }, sawUnavailable, drainEndedAt };
	}
	if (
		inferStoppedReplacement
		&& drainEndedAt !== null
		&& !sawUnavailable
		&& Date.now() - drainEndedAt >= 2_000
	) {
		return {
			phase,
			result: {
				ok: false,
				message: mode === 'update'
					? 'Update stopped during restart; the previous server is still running. No replacement was verified. Inspect the restart log before retrying.'
					: 'Restart stopped before replacing the server. Inspect the restart log before retrying.',
			},
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

function completionUnverifiedMessage(mode: 'restart' | 'update'): string {
	return mode === 'update'
		? "Couldn't verify update completion. Inspect the restart log before retrying."
		: "Couldn't verify restart completion. Inspect the restart log before retrying.";
}

export async function waitForSystemRestart(
	mode: 'restart' | 'update',
	before: SystemVersionSnapshot | null,
	operationId: string,
	onPhase?: (message: string) => void,
	timeoutMs = SYSTEM_RESTART_TIMEOUT_MS,
): Promise<{ ok: boolean; message?: string }> {
	const deadline = Date.now() + timeoutMs;
	let sawUnavailable = false;
	let drainEndedAt: number | null = null;
	while (Date.now() < deadline) {
		const observation = await observeSystemRestartWithin(operationId, deadline - Date.now());
		if (!observation || Date.now() >= deadline) break;
		const outcome = classifyRestartObservation(mode, before, observation, sawUnavailable, drainEndedAt);
		sawUnavailable = outcome.sawUnavailable;
		drainEndedAt = outcome.drainEndedAt;
		if (outcome.phase) onPhase?.(outcome.phase);
		if (outcome.result) {
			if (outcome.result.ok) onPhase?.('Restart complete. Reloading…');
			return outcome.result;
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}

	onPhase?.(mode === 'update' ? 'Checking update result…' : 'Checking restart result…');
	const finalObservation = await observeSystemRestartWithin(operationId, FINAL_RESTART_OBSERVATION_TIMEOUT_MS);
	if (finalObservation) {
		// At the deadline, only explicit failure or strict completion can settle the fresh observation.
		const outcome = classifyRestartObservation(mode, before, finalObservation, sawUnavailable, drainEndedAt, false);
		if (outcome.phase) onPhase?.(outcome.phase);
		if (outcome.result) {
			if (outcome.result.ok) onPhase?.('Restart complete. Reloading…');
			return outcome.result;
		}
	}
	return { ok: false, message: completionUnverifiedMessage(mode) };
}
