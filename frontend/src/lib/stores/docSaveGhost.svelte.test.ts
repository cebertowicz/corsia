import { flushSync } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { docSaveGhost } from './docSaveGhost.svelte';
import type { DocSaveStatus } from './docSaveQueue';

afterEach(() => vi.useRealTimers());

describe('docSaveGhost', () => {
	it('shows saving, then saved for 1400ms, then idles', () => {
		vi.useFakeTimers();
		let status = $state<DocSaveStatus | undefined>('saving');
		let ghost!: ReturnType<typeof docSaveGhost>;
		const teardown = $effect.root(() => { ghost = docSaveGhost(() => status); });
		flushSync();
		expect(ghost.current).toBe('saving');
		status = 'saved';
		flushSync();
		expect(ghost.current).toBe('saved');
		vi.advanceTimersByTime(1399);
		expect(ghost.current).toBe('saved');
		vi.advanceTimersByTime(1);
		flushSync();
		expect(ghost.current).toBe('idle');
		teardown();
	});

	it('hides the badge at once when a new edit starts', () => {
		vi.useFakeTimers();
		let status = $state<DocSaveStatus | undefined>('saved');
		let ghost!: ReturnType<typeof docSaveGhost>;
		const teardown = $effect.root(() => { ghost = docSaveGhost(() => status); });
		flushSync();
		expect(ghost.current).toBe('saved');
		status = 'dirty';
		flushSync();
		expect(ghost.current).toBe('idle');
		teardown();
	});
});
