import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { toasts } from './toast';

function clearToasts() {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
}

beforeEach(() => {
	vi.useFakeTimers();
	clearToasts();
});

afterEach(() => {
	clearToasts();
	vi.useRealTimers();
});

function ids() {
	return get(toasts).map((toast) => toast.id);
}

describe('ordinary toasts', () => {
	it('keep the shipped lifetimes', async () => {
		toasts.show('Saved');
		expect(get(toasts)[0]?.duration).toBe(3000);
		await vi.advanceTimersByTimeAsync(3001);
		expect(get(toasts)).toEqual([]);
	});

	it('are capped at one: a newer toast replaces the older one', async () => {
		toasts.show('First');
		toasts.showAction('Second', { label: 'Undo', run: vi.fn() });
		toasts.show('Third', 'error');

		expect(get(toasts).map((toast) => toast.message)).toEqual(['Third']);
		expect(get(toasts)[0]?.type).toBe('error');
	});

	it('drop the replaced toast timer so it cannot dismiss its successor', async () => {
		toasts.show('First', 'info', 1000);
		await vi.advanceTimersByTimeAsync(900);
		toasts.show('Second', 'info', 5000);
		await vi.advanceTimersByTimeAsync(200);
		expect(get(toasts).map((toast) => toast.message)).toEqual(['Second']);
	});
});

describe('pinned slot', () => {
	it('is never dropped by ordinary toasts and always sits last', () => {
		toasts.pin('update', { type: 'progress', message: 'Stopping agents' });
		toasts.show('Archived');
		toasts.showAction('Moved', { label: 'Undo', run: vi.fn() });

		expect(get(toasts).map((toast) => toast.message)).toEqual(['Moved', 'Stopping agents']);
		expect(get(toasts)[1]?.pinned?.key).toBe('update');
	});

	it('puts an ordinary toast that arrives later above the pinned one', () => {
		toasts.show('Archived');
		toasts.pin('update', { type: 'progress', message: 'Getting the update' });
		expect(get(toasts).map((toast) => toast.message)).toEqual(['Archived', 'Getting the update']);
	});

	it('updates in place: same element, new text, tone and action', () => {
		const id = toasts.pin('update', {
			type: 'progress',
			message: 'Getting the update',
			time: '0:01',
			closeLabel: 'Hide progress',
		});
		const run = vi.fn();
		const again = toasts.pin('update', {
			type: 'progress',
			message: 'Stopping agents · 2 of 3 safe',
			time: '0:18',
			closeLabel: 'Hide progress',
			action: { label: 'Details', run },
		});

		expect(again).toBe(id);
		expect(get(toasts)).toHaveLength(1);
		expect(get(toasts)[0]).toMatchObject({
			message: 'Stopping agents · 2 of 3 safe',
			type: 'progress',
			action: { label: 'Details' },
			pinned: { key: 'update', time: '0:18', closeLabel: 'Hide progress' },
		});
	});

	it('has no timeout while it shows progress', async () => {
		toasts.pin('update', { type: 'progress', message: 'Restarting' });
		await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
		expect(get(toasts)).toHaveLength(1);
	});

	it('is replaced by a success outcome without a second element, then frees the slot', async () => {
		const id = toasts.pin('update', { type: 'progress', message: 'Restarting' });
		toasts.pin('update', { type: 'success', message: 'Updated to 2026.10.11' });

		expect(ids()).toEqual([id]);
		expect(get(toasts)[0]?.type).toBe('success');
		await vi.advanceTimersByTimeAsync(7999);
		expect(get(toasts)).toHaveLength(1);
		await vi.advanceTimersByTimeAsync(2);
		expect(get(toasts)).toEqual([]);
	});

	it('is replaced by an error outcome that stays until dismissed', async () => {
		const id = toasts.pin('update', { type: 'progress', message: 'Restarting' });
		toasts.pin('update', {
			type: 'error',
			message: 'Update stopped',
			action: { label: 'Details', run: vi.fn() },
		});

		expect(ids()).toEqual([id]);
		await vi.advanceTimersByTimeAsync(60 * 1000);
		expect(get(toasts)).toHaveLength(1);
		toasts.dismiss(id);
		expect(get(toasts)).toEqual([]);
	});

	it('stops a progress toast timer when an outcome that times out replaces it, and the reverse', async () => {
		toasts.pin('update', { type: 'success', message: 'Updated', duration: 1000 });
		toasts.pin('update', { type: 'progress', message: 'Restarting' });
		await vi.advanceTimersByTimeAsync(5000);
		expect(get(toasts)).toHaveLength(1);
	});

	it('keeps an outcome open while the pointer is inside it', async () => {
		const id = toasts.pin('update', { type: 'progress', message: 'Restarting' });
		toasts.pause(id, 'pointer');
		toasts.pin('update', { type: 'success', message: 'Updated', duration: 1000 });
		await vi.advanceTimersByTimeAsync(5000);
		expect(get(toasts)).toHaveLength(1);
		toasts.resume(id, 'pointer');
		await vi.advanceTimersByTimeAsync(1001);
		expect(get(toasts)).toEqual([]);
	});

	it('lets a different key take over the one slot', () => {
		const first = toasts.pin('update', { type: 'progress', message: 'Restarting' });
		const second = toasts.pin('other', { type: 'progress', message: 'Importing' });

		expect(second).not.toBe(first);
		expect(get(toasts).map((toast) => toast.message)).toEqual(['Importing']);
	});

	it('unpin removes the toast owned by the key only', () => {
		toasts.pin('update', { type: 'progress', message: 'Restarting' });
		toasts.unpin('other');
		expect(get(toasts)).toHaveLength(1);
		toasts.unpin('update');
		expect(get(toasts)).toEqual([]);
	});

	it('runs its action once and steps aside', async () => {
		const run = vi.fn();
		const id = toasts.pin('update', {
			type: 'progress',
			message: 'Restarting',
			action: { label: 'Details', run },
		});
		await Promise.all([toasts.runAction(id), toasts.runAction(id)]);
		expect(run).toHaveBeenCalledTimes(1);
		expect(get(toasts)).toEqual([]);
	});

	it('keeps a pinned failure visible when Details opens', async () => {
		const run = vi.fn();
		const id = toasts.pin('update', {
			type: 'error',
			message: "Update didn't start",
			action: { label: 'Details', run, dismiss: false },
		});
		await toasts.runAction(id);
		expect(run).toHaveBeenCalledOnce();
		expect(get(toasts)).toHaveLength(1);
	});
});

describe('action toasts', () => {
	it('carry one action that dismisses and cannot run twice', async () => {
		const run = vi.fn().mockResolvedValue(undefined);
		const store = toasts as any;

		store.showAction('Removed from pane', { label: 'Undo', run });
		const toast = get(toasts)[0] as any;

		expect(toast.action.label).toBe('Undo');
		await Promise.all([store.runAction(toast.id), store.runAction(toast.id)]);
		expect(run).toHaveBeenCalledTimes(1);
		expect(get(toasts)).toEqual([]);
	});

	it('uses the longer eight-second lifetime by default', () => {
		(toasts as any).showAction('Removed from pane', { label: 'Undo', run: vi.fn() });
		expect(get(toasts)[0]?.duration).toBe(8000);
	});
});
