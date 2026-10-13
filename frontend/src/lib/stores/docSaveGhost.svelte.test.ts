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
		const teardown = $effect.root(() => { ghost = docSaveGhost(() => status, () => 'tab-a'); });
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
		let status = $state<DocSaveStatus | undefined>('saving');
		let ghost!: ReturnType<typeof docSaveGhost>;
		const teardown = $effect.root(() => { ghost = docSaveGhost(() => status, () => 'tab-a'); });
		flushSync();
		status = 'saved';
		flushSync();
		expect(ghost.current).toBe('saved');
		status = 'dirty';
		flushSync();
		expect(ghost.current).toBe('idle');
		teardown();
	});

	it('stays quiet on a switch to a draft that saved before it was watched, then shows its next save', () => {
		vi.useFakeTimers();
		let draft = $state<{ key: string; status: DocSaveStatus }>({ key: 'tab-b', status: 'idle' });
		let ghost!: ReturnType<typeof docSaveGhost>;
		const teardown = $effect.root(() => { ghost = docSaveGhost(() => draft.status, () => draft.key); });
		flushSync();
		draft = { key: 'tab-a', status: 'saved' };
		flushSync();
		expect(ghost.current).toBe('idle');
		// The same saved draft read again (a patch of another field) is still no save.
		draft = { key: 'tab-a', status: 'saved' };
		flushSync();
		expect(ghost.current).toBe('idle');
		draft = { key: 'tab-a', status: 'saving' };
		flushSync();
		draft = { key: 'tab-a', status: 'saved' };
		flushSync();
		expect(ghost.current).toBe('saved');
		teardown();
	});

	it('stays quiet when first mounted on a draft that is already saved', () => {
		let ghost!: ReturnType<typeof docSaveGhost>;
		const teardown = $effect.root(() => { ghost = docSaveGhost(() => 'saved', () => 'tab-a'); });
		flushSync();
		expect(ghost.current).toBe('idle');
		teardown();
	});

	it('keeps a shown badge and its timer when the same saved draft is read again', () => {
		vi.useFakeTimers();
		let draft = $state<{ key: string; status: DocSaveStatus }>({ key: 'tab-a', status: 'saving' });
		let ghost!: ReturnType<typeof docSaveGhost>;
		const teardown = $effect.root(() => { ghost = docSaveGhost(() => draft.status, () => draft.key); });
		flushSync();
		draft = { key: 'tab-a', status: 'saved' };
		flushSync();
		vi.advanceTimersByTime(1000);
		draft = { key: 'tab-a', status: 'saved' };
		flushSync();
		expect(ghost.current).toBe('saved');
		vi.advanceTimersByTime(400);
		flushSync();
		expect(ghost.current).toBe('idle');
		teardown();
	});
});
