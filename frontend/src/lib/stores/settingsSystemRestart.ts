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
	failure_code?: 'disk_space' | 'fetch_failed' | 'build_failed' | 'launch_failed' | 'drain_cancelled' | null;
	failure_detail?: string | null;
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
	terminalMismatchAt: number | null;
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

function revisionMismatchMessage(
	backendSha: string | null | undefined,
	frontendSha: string | null | undefined,
	checkoutSha: string | null | undefined,
): string {
	const mismatches = [
		(!backendSha || backendSha === 'unknown') && 'backend',
		(!frontendSha || frontendSha === 'unknown' || frontendSha !== backendSha) && 'frontend',
		(checkoutSha && (checkoutSha === 'unknown' || checkoutSha !== backendSha)) && 'checkout',
	].filter(Boolean).join(', ');
	return `Update finished, but the running versions do not match (${mismatches}). CORSIA is still running. Inspect Details before retrying.`;
}

function classifyRestartObservation(
	mode: 'restart' | 'update',
	before: SystemVersionSnapshot | null,
	observation: RestartObservation,
	sawUnavailable: boolean,
	drainEndedAt: number | null,
	terminalMismatchAt: number | null,
	inferStoppedReplacement = true,
): RestartObservationResult {
	const { version, restartRead } = observation;
	if (typeof restartRead === 'string') {
		return { result: { ok: false, message: restartRead }, sawUnavailable, drainEndedAt, terminalMismatchAt };
	}
	if (['failed', 'cancelled', 'superseded'].includes(restartRead?.status ?? '')) {
		return {
			result: { ok: false, message: restartFailureMessage(restartRead) },
			sawUnavailable,
			drainEndedAt,
			terminalMismatchAt,
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
		terminalMismatchAt = null;
	} else if (!version) {
		sawUnavailable = true;
	} else if (restartRead) {
		drainEndedAt ??= Date.now();
	}

	const phase = restartPhase(restartRead, Boolean(version), restarted, revisionsAgree);
	if (restarted && revisionsAgree && restartRead?.status === 'complete') {
		return { phase, result: { ok: true }, sawUnavailable, drainEndedAt, terminalMismatchAt: null };
	}
	if (restarted && !revisionsAgree && restartRead?.status === 'complete') {
		terminalMismatchAt ??= Date.now();
		if (Date.now() - terminalMismatchAt >= 10_000) {
			return {
				phase,
				result: { ok: false, message: revisionMismatchMessage(backendSha, frontendSha, checkoutSha) },
				sawUnavailable,
				drainEndedAt,
				terminalMismatchAt,
			};
		}
	} else {
		terminalMismatchAt = null;
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
			result: {
				ok: false,
				message: mode === 'update'
					? 'Update stopped during restart; the previous server is still running. No replacement was verified. Inspect the restart log before retrying.'
					: 'Restart stopped before replacing the server. Inspect the restart log before retrying.',
			},
			sawUnavailable,
			drainEndedAt,
			terminalMismatchAt,
		};
	}
	return { phase, sawUnavailable, drainEndedAt, terminalMismatchAt };
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

function restartFailureMessage(status: SystemRestartStatus | null | undefined): string {
	if (status?.failure_code === 'disk_space') {
		try {
			const detail = JSON.parse(status.failure_detail ?? '{}');
			const needed = Math.round(Number(detail.needed_bytes) / 1e9);
			const free = Math.round(Number(detail.free_bytes) / 1e9);
			if (Number.isFinite(needed) && Number.isFinite(free)) {
				return `Needs ${needed} GB free; this Mac has ${free} GB.`;
			}
		} catch {
			// The durable summary remains the safe fallback for malformed legacy detail.
		}
	}
	const messages: Record<string, string> = {
		fetch_failed: 'Could not download the update. CORSIA is still running.',
		build_failed: 'Could not build the update. CORSIA is still running.',
		launch_failed: 'Could not start the restart. CORSIA is still running.',
		drain_cancelled: 'Update cancelled before restart.',
	};
	return (status?.failure_code && messages[status.failure_code])
		?? status?.error
		?? 'Restart operation ended before a replacement was verified.';
}

export async function waitForSystemRestart(
	mode: 'restart' | 'update',
	before: SystemVersionSnapshot | null,
	operationId: string,
	onPhase?: (message: string) => void,
): Promise<{ ok: boolean; message?: string }> {
	let sawUnavailable = false;
	let drainEndedAt: number | null = null;
	let terminalMismatchAt: number | null = null;
	while (true) {
		const observation = await observeSystemRestartWithin(operationId, 5_000);
		if (observation === null) {
			sawUnavailable = true;
			onPhase?.('Restarting backend…');
			await new Promise((resolve) => setTimeout(resolve, 500));
			continue;
		}
		const outcome = classifyRestartObservation(
			mode, before, observation, sawUnavailable, drainEndedAt, terminalMismatchAt,
		);
		sawUnavailable = outcome.sawUnavailable;
		drainEndedAt = outcome.drainEndedAt;
		terminalMismatchAt = outcome.terminalMismatchAt;
		if (outcome.phase) onPhase?.(outcome.phase);
		if (outcome.result) {
			if (outcome.result.ok) onPhase?.('Restart complete. Reloading…');
			return outcome.result;
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
}
