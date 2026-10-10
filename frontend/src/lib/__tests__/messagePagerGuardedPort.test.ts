import { describe, it, expect, vi } from 'vitest';
import {
	makeGuardedPort,
	PAGER_ABORT,
	type MessagePage,
	type PagerPort,
} from '../messagePager';

function makeBase(page: MessagePage | null = null): PagerPort & { prependCalls: any[][] } {
	const calls: any[][] = [];
	return {
		fetchPage: async () => page,
		prepend: (older: any[]) => {
			calls.push(older);
		},
		getScrollEl: () => ({ scrollTop: 0, scrollHeight: 100, clientHeight: 50 }),
		afterRender: async () => {},
		prependCalls: calls,
	};
}

describe('makeGuardedPort', () => {
	it('delegates fetchPage when generation matches', async () => {
		let gen = 1;
		const page: MessagePage = { messages: [{ id: 'a' }], has_more: false, oldest_id: null };
		const guarded = makeGuardedPort(() => gen, 1, makeBase(page));
		const result = await guarded.fetchPage('cursor');
		expect(result).toBe(page);
	});

	it('returns PAGER_ABORT when generation has advanced before fetch', async () => {
		let gen = 2;
		const guarded = makeGuardedPort(() => gen, 1, makeBase());
		const result = await guarded.fetchPage('cursor');
		expect(result).toBe(PAGER_ABORT);
	});

	it('returns PAGER_ABORT when generation advances mid-fetch', async () => {
		let gen = 1;
		let resolveOuter!: (v: MessagePage | null) => void;
		const base: PagerPort = {
			fetchPage: () => new Promise(r => { resolveOuter = r; }),
			prepend: vi.fn(),
			getScrollEl: () => null,
			afterRender: async () => {},
		};
		const guarded = makeGuardedPort(() => gen, 1, base);
		const pending = guarded.fetchPage('cursor');
		gen = 2;
		resolveOuter(null);
		expect(await pending).toBe(PAGER_ABORT);
	});

	it('no-ops prepend when generation has advanced', () => {
		let gen = 2;
		const base = makeBase();
		const guarded = makeGuardedPort(() => gen, 1, base);
		guarded.prepend([{ id: 'x' }]);
		expect(base.prependCalls).toHaveLength(0);
	});

	it('returns null from getScrollEl when generation has advanced', () => {
		let gen = 2;
		const guarded = makeGuardedPort(() => gen, 1, makeBase());
		expect(guarded.getScrollEl()).toBeNull();
	});
});
