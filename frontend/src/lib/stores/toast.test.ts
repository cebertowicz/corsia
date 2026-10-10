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
