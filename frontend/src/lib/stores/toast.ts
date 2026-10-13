import { get, writable } from 'svelte/store';

export interface ToastAction {
	label: string;
	run: () => void | Promise<unknown>;
}

export type OrdinaryToastType = 'info' | 'success' | 'error';
export type ToastType = OrdinaryToastType | 'progress';

/** Present only on the toast that occupies the pinned slot. */
export interface PinnedToastState {
	key: string;
	/** Accessible name and tooltip for the close button; defaults to "Dismiss". */
	closeLabel?: string;
	/** A ticking segment shown after the message, dropped first when the line does not fit. */
	time?: string;
}

export interface Toast {
	id: string;
	message: string;
	type?: ToastType;
	duration?: number;
	action?: ToastAction;
	pinned?: PinnedToastState;
}

export interface PinnedToastSpec {
	message: string;
	/**
	 * `progress` has no timeout and shows a spinner. `error` stays until it is
	 * dismissed. `success` and `info` are outcomes: they last `duration` and then
	 * free the slot.
	 */
	type: ToastType;
	action?: ToastAction;
	closeLabel?: string;
	time?: string;
	/** Lifetime of a `success` or `info` outcome in ms. Default 8000. */
	duration?: number;
}

interface ToastTimer {
	handle: ReturnType<typeof setTimeout> | null;
	remaining: number;
	startedAt: number;
}

const ORDINARY_MS = 3000;
const ACTION_MS = 8000;

function newId(): string {
	return Math.random().toString(36).slice(2);
}

/**
 * The stack holds at most one ordinary toast and the pinned slot. A newer
 * ordinary toast replaces the older one; the pinned toast is never dropped to
 * make room and always sits last, nearest the composer.
 */
function createToastStore() {
	const store = writable<Toast[]>([]);
	const timers = new Map<string, ToastTimer>();
	const pauseReasons = new Map<string, Set<string>>();
	const runningActions = new Set<string>();

	function clearTimer(id: string) {
		const timer = timers.get(id);
		if (timer?.handle) clearTimeout(timer.handle);
		timers.delete(id);
	}

	function forget(id: string) {
		clearTimer(id);
		pauseReasons.delete(id);
	}

	function schedule(id: string, remaining: number) {
		clearTimer(id);
		const timer: ToastTimer = { handle: null, remaining, startedAt: Date.now() };
		timer.handle = setTimeout(() => dismiss(id), remaining);
		timers.set(id, timer);
	}

	/** Start a lifetime, staying paused if the pointer or focus is already inside. */
	function arm(id: string, duration: number) {
		if (pauseReasons.get(id)?.size) {
			clearTimer(id);
			timers.set(id, { handle: null, remaining: duration, startedAt: Date.now() });
			return;
		}
		schedule(id, duration);
	}

	function addOrdinary(toast: Toast, duration: number) {
		for (const old of get(store)) {
			if (!old.pinned) forget(old.id);
		}
		store.update((items) => [toast, ...items.filter((item) => item.pinned)]);
		schedule(toast.id, duration);
		return toast.id;
	}

	function show(message: string, type: OrdinaryToastType = 'info', duration = ORDINARY_MS) {
		return addOrdinary({ id: newId(), message, type, duration }, duration);
	}

	function showAction(message: string, action: ToastAction) {
		return addOrdinary(
			{ id: newId(), message, type: 'info', duration: ACTION_MS, action },
			ACTION_MS,
		);
	}

	/**
	 * Show, update in place, or replace with an outcome the toast owned by `key`.
	 * The same key always keeps one element; a different key takes over the slot.
	 */
	function pin(key: string, spec: PinnedToastSpec) {
		const current = get(store).find((item) => item.pinned);
		const id = current?.pinned?.key === key ? current.id : newId();
		if (current && current.id !== id) forget(current.id);
		const persistent = spec.type === 'progress' || spec.type === 'error';
		const duration = persistent ? undefined : (spec.duration ?? ACTION_MS);
		const toast: Toast = {
			id,
			message: spec.message,
			type: spec.type,
			duration,
			action: spec.action,
			pinned: { key, closeLabel: spec.closeLabel, time: spec.time },
		};
		store.update((items) => [...items.filter((item) => !item.pinned), toast]);
		if (duration === undefined) clearTimer(id);
		else arm(id, duration);
		return id;
	}

	function unpin(key: string) {
		const current = get(store).find((item) => item.pinned?.key === key);
		if (current) dismiss(current.id);
	}

	function dismiss(id: string) {
		forget(id);
		store.update((items) => items.filter((toast) => toast.id !== id));
	}

	function pause(id: string, reason = 'interaction') {
		const reasons = pauseReasons.get(id) ?? new Set<string>();
		reasons.add(reason);
		pauseReasons.set(id, reasons);
		const timer = timers.get(id);
		if (!timer?.handle) return;
		clearTimeout(timer.handle);
		timer.remaining = Math.max(0, timer.remaining - (Date.now() - timer.startedAt));
		timer.handle = null;
	}

	function resume(id: string, reason = 'interaction') {
		const reasons = pauseReasons.get(id);
		reasons?.delete(reason);
		if (reasons?.size) return;
		pauseReasons.delete(id);
		const timer = timers.get(id);
		if (!timer || timer.handle) return;
		schedule(id, timer.remaining);
	}

	async function runAction(id: string) {
		if (runningActions.has(id)) return;
		const action = get(store).find((toast) => toast.id === id)?.action;
		if (!action) return;
		runningActions.add(id);
		dismiss(id);
		try {
			await action.run();
		} finally {
			runningActions.delete(id);
		}
	}

	return {
		subscribe: store.subscribe,
		show,
		showAction,
		pin,
		unpin,
		dismiss,
		pause,
		resume,
		runAction,
	};
}

export const toasts = createToastStore();
