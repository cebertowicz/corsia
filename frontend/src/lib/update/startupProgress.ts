import { writable } from 'svelte/store';
import { readStartupProgress, shellInfo } from '$lib/remoteShell';

export type StartupPhase = 'starting' | 'backing_up' | 'migrating' | 'validating' | 'recovering' | 'serving' | 'failed';

/** The redacted, PID-validated startup-progress record the desktop shell returns. */
export interface StartupProgress {
	schema: 1;
	pid: number;
	seq: number;
	phase: StartupPhase;
	done: number | null;
	total: number | null;
}

const PHASES: readonly string[] = ['starting', 'backing_up', 'migrating', 'validating', 'recovering', 'serving', 'failed'];
const count = (value: unknown): number | null => (Number.isFinite(value) && (value as number) >= 0 ? (value as number) : null);

/** Only a whole, current-schema object counts; anything else is "nothing yet". */
export function parseStartupProgress(raw: unknown): StartupProgress | null {
	if (!raw || typeof raw !== 'object') return null;
	const value = raw as Record<string, unknown>;
	if (value.schema !== 1 || typeof value.phase !== 'string' || !PHASES.includes(value.phase)) return null;
	if (!Number.isInteger(value.pid) || !Number.isInteger(value.seq)) return null;
	return {
		schema: 1, pid: value.pid as number, seq: value.seq as number, phase: value.phase as StartupPhase,
		done: count(value.done), total: count(value.total),
	};
}

/** The address the shell gives its update page once the service answers and no update holds it; checked again before use. */
function parseReadyUrl(raw: unknown): string | null {
	const url = raw && typeof raw === 'object' ? (raw as Record<string, unknown>).ready_url : null;
	return typeof url === 'string' && url ? url : null;
}

/** The latest read: an absent file is valid; an unavailable reader is distinct and exposes only a sanitized diagnostic. */
export const startupProgress = writable<{ file: StartupProgress | null; readAt: number; readError: 'unavailable' | null; readyUrl?: string | null } | null>(null);
let generation = 0;
let readerFailureReported = false;

/**
 * The service address a reopened app's own launch start answered Ready with, as the shell draws it on its update page
 * (`shellEntry.ts`). Followed like the reading's `readyUrl`; it is checked the same way and grants nothing.
 */
export const launchReadyUrl = writable<string | null>(null);

/** One read through the shell's command; a browser or remote shell has no file to read. */
export async function refreshStartupProgress(): Promise<void> {
	if (shellInfo()?.mode !== 'local') return;
	const mine = ++generation;
	let file: StartupProgress | null = null;
	let readyUrl: string | null = null;
	let readError: 'unavailable' | null = null;
	try {
		const reading = await readStartupProgress();
		file = parseStartupProgress(reading);
		readyUrl = parseReadyUrl(reading);
		readerFailureReported = false;
	} catch {
		readError = 'unavailable';
		if (!readerFailureReported) {
			console.warn('[update] startup progress is unavailable');
			readerFailureReported = true;
		}
	}
	if (mine === generation) startupProgress.set({ file, readAt: Date.now(), readError, readyUrl });
}

export function clearStartupProgress(): void {
	generation += 1;
	readerFailureReported = false;
	startupProgress.set(null);
}
