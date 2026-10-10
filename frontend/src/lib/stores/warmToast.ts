import { get, writable } from 'svelte/store';

export interface WarmProgress {
	totalBlocks: number;
	coldBlocksAtStart: number;
	blockSize: number;
}

export interface WarmToastState {
	visible: boolean;
	message: string;
	status: 'warming' | 'done' | 'rebuilding';
	elapsed: number;
	progress: WarmProgress | null;
}

export const warmToast = writable<WarmToastState>({
	visible: false,
	message: '',
	status: 'warming',
	elapsed: 0,
	progress: null,
});

/**
 * Longest a cache-rebuild spinner may stay on screen without ANY word from the
 * turn it belongs to. Normal completion is `settleRebuildToast`; this only
 * fires when no admitted frame ever arrives.
 */
export const REBUILD_TOAST_CEILING_MS = 60_000;

let timer: ReturnType<typeof setInterval> | null = null;
let dismissTimer: ReturnType<typeof setTimeout> | null = null;

export function showWarmToast(
	message: string,
	status: 'warming' | 'done' | 'rebuilding' = 'warming',
	autoDismissMs?: number,
	progress: WarmProgress | null = null,
) {
	if (timer) { clearInterval(timer); timer = null; }
	if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }

	warmToast.set({ visible: true, message, status, elapsed: 0, progress });

	if (status === 'warming' || status === 'rebuilding') {
		timer = setInterval(() => {
			warmToast.update(s => ({ ...s, elapsed: s.elapsed + 1 }));
		}, 1000);
	}

	if (autoDismissMs) {
		dismissTimer = setTimeout(dismissWarmToast, autoDismissMs);
	}
}

export function completeWarmToast(durationSecs: number) {
	if (timer) { clearInterval(timer); timer = null; }
	warmToast.set({
		visible: true,
		message: `Cache ready (${durationSecs}s)`,
		status: 'done',
		elapsed: durationSecs,
		progress: null,
	});
	dismissTimer = setTimeout(dismissWarmToast, 3000);
}

/**
 * End a `rebuilding` spinner because the operation it describes is over.
 *
 * A rebuild is a cache prefill that happens *inside* the next turn, so the turn
 * speaking its first token — or ending without one — IS its completion. The
 * `warming` toast is a different operation (an HTTP warm call that reports its
 * own `Cache ready (Ns)`), so it is deliberately left alone here.
 */
export function settleRebuildToast() {
	const current = get(warmToast);
	if (current.visible && current.status === 'rebuilding') dismissWarmToast();
}

export function dismissWarmToast() {
	if (timer) { clearInterval(timer); timer = null; }
	if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }
	warmToast.set({ visible: false, message: '', status: 'warming', elapsed: 0, progress: null });
}
