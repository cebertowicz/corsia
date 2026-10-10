import { get, writable } from 'svelte/store';

export interface ToastAction {
	label: string;
	run: () => void | Promise<unknown>;
}

export interface Toast {
	id: string;
	message: string;
	type?: 'info' | 'success' | 'error';
	duration?: number;
	action?: ToastAction;
}

interface ToastTimer {
	handle: ReturnType<typeof setTimeout> | null;
	remaining: number;
	startedAt: number;
}

function createToastStore() {
	const store = writable<Toast[]>([]);
	const timers = new Map<string, ToastTimer>();
	const pauseReasons = new Map<string, Set<string>>();
	const runningActions = new Set<string>();

	function schedule(id: string, remaining: number) {
		const timer: ToastTimer = { handle: null, remaining, startedAt: Date.now() };
		timer.handle = setTimeout(() => dismiss(id), remaining);
		timers.set(id, timer);
	}

	function show(message: string, type: Toast['type'] = 'info', duration = 3000) {
		const id = Math.random().toString(36).slice(2);
		store.update((items) => [...items, { id, message, type, duration }]);
		schedule(id, duration);
		return id;
	}

	function showAction(message: string, action: ToastAction) {
		const id = Math.random().toString(36).slice(2);
		const duration = 8000;
		store.update((items) => [
			...items,
			{ id, message, type: 'info', duration, action },
		]);
		schedule(id, duration);
		return id;
	}

	function dismiss(id: string) {
		const timer = timers.get(id);
		if (timer?.handle) clearTimeout(timer.handle);
		timers.delete(id);
		pauseReasons.delete(id);
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
		dismiss,
		pause,
		resume,
		runAction,
	};
}

export const toasts = createToastStore();
