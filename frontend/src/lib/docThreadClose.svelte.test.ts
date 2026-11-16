import { flushSync, tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import type { DocComment } from '$lib/stores/docComments';
import { closeOnOwnResolve } from './docThreadClose.svelte';

const thread = (resolved: boolean): DocComment => ({ id: 'c1', from: 0, to: 5, anchor: 'alpha', author: 'you', text: 'hello', at: 'now', resolved, replies: [] });

function setup(hasDraft = false) {
	let head = $state.raw<DocComment | undefined>(thread(false));
	const close = vi.fn();
	let own!: ReturnType<typeof closeOnOwnResolve>;
	const stop = $effect.root(() => { own = closeOnOwnResolve(() => head, () => hasDraft, close); });
	flushSync();
	return { own, close, stop, set(next: DocComment | undefined) { head = next; flushSync(); } };
}

describe('closing a phone thread sheet on the person’s own Resolve', () => {
	it('closes once their Resolve takes the thread from open to resolved', async () => {
		const test = setup();
		try {
			let answer!: (ok: boolean) => void;
			test.own.resolving('c1', new Promise<boolean>(resolve => { answer = resolve; }));
			test.set(thread(true));
			expect(test.close).toHaveBeenCalledOnce();
			answer(true); await tick();
		} finally { test.stop(); }
	});

	it('leaves the sheet up when someone else resolves the thread after an Agree, which sets no mark', () => {
		const test = setup();
		try {
			// Agree never calls resolving(), so another party's later resolve is not the person's own.
			test.set(thread(true));
			expect(test.close).not.toHaveBeenCalled();
		} finally { test.stop(); }
	});

	it('leaves the sheet up when someone else resolves after the person’s Resolve was refused', async () => {
		const test = setup();
		try {
			test.own.resolving('c1', Promise.resolve(false));
			await tick(); await tick();
			test.set(thread(true));
			expect(test.close).not.toHaveBeenCalled();
		} finally { test.stop(); }
	});

	it('leaves the sheet up when someone else resolves after the person’s Resolve settled with the thread still open', async () => {
		const test = setup();
		try {
			test.own.resolving('c1', Promise.resolve(true));
			await tick(); await tick();
			test.set(thread(true));
			expect(test.close).not.toHaveBeenCalled();
		} finally { test.stop(); }
	});

	it('keeps the sheet up for an unsent reply even on the person’s own Resolve', () => {
		const test = setup(true);
		try {
			test.own.resolving('c1', new Promise(() => {}));
			test.set(thread(true));
			expect(test.close).not.toHaveBeenCalled();
		} finally { test.stop(); }
	});
});
