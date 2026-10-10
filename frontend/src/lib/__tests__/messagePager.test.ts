import { describe, it, expect, vi } from 'vitest';
import {
	createPager,
	adoptFirstPage,
	shouldLoadOlder,
	loadOlder,
	LOAD_OLDER_TRIGGER_PX,
	MAX_PAGER_CHAIN,
	PAGER_ABORT,
	type MessagePage,
	type PagerPort,
	type ScrollAnchor,
} from '../messagePager';

function makeScrollEl(opts: { perMsgPx?: number; clientHeight?: number } = {}) {
	const perMsg = opts.perMsgPx ?? 20;
	const el = {
		scrollTop: 0,
		clientHeight: opts.clientHeight ?? 100,
		_count: 0,
		get scrollHeight() {
			return this._count * perMsg;
		},
	};
	return el as ScrollAnchor & { _count: number };
}

function pageOf(ids: string[], hasMore: boolean): MessagePage {
	return {
		messages: ids.map((id) => ({ id, content: id })),
		has_more: hasMore,
		oldest_id: ids.length ? ids[0] : null,
	};
}

describe('shouldLoadOlder', () => {
	it('fires only inside the trigger band with more to load', () => {
		const s = createPager({ hasMore: true, oldestId: 'a' });
		expect(shouldLoadOlder(s, 0)).toBe(true);
		expect(shouldLoadOlder(s, LOAD_OLDER_TRIGGER_PX - 1)).toBe(true);
		expect(shouldLoadOlder(s, LOAD_OLDER_TRIGGER_PX + 1)).toBe(false);
	});

	it('does not fire when no more pages or already loading', () => {
		expect(shouldLoadOlder(createPager({ hasMore: false }), 0)).toBe(false);
		const loading = createPager({ hasMore: true, oldestId: 'a' });
		loading.loading = true;
		expect(shouldLoadOlder(loading, 0)).toBe(false);
	});
});

describe('adoptFirstPage', () => {
	it('seeds cursor + hasMore from the first page and clears flags', () => {
		const s = createPager();
		s.loading = true;
		s.error = true;
		adoptFirstPage(s, pageOf(['m20', 'm21', 'm22'], true));
		expect(s.hasMore).toBe(true);
		expect(s.oldestId).toBe('m20');
		expect(s.loading).toBe(false);
		expect(s.error).toBe(false);
	});
});

describe('loadOlder — anchor + cursor', () => {
	it('prepends older messages and preserves the reading position', async () => {
		const el = makeScrollEl({ perMsgPx: 20 });
		el._count = 10;
		el.scrollTop = 50;
		const prepend = vi.fn((older: any[]) => {
			el._count += older.length;
		});
		const port: PagerPort = {
			fetchPage: vi.fn(async () => pageOf(['o5', 'o6', 'o7', 'o8', 'o9'], true)),
			prepend,
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'm0' });

		const n = await loadOlder(s, port, 1);
		expect(n).toBe(5);
		expect(prepend).toHaveBeenCalledOnce();
		expect(el.scrollTop).toBe(150);
		expect(s.oldestId).toBe('o5');
		expect(s.hasMore).toBe(true);
	});

	it('advances the cursor across sequential pages', async () => {
		const el = makeScrollEl();
		el.scrollTop = 500;
		const port: PagerPort = {
			fetchPage: vi
				.fn()
				.mockResolvedValueOnce(pageOf(['b1', 'b2'], true))
				.mockResolvedValueOnce(pageOf(['c1', 'c2'], false)),
			prepend: (older) => {
				el._count += older.length;
			},
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'd1' });

		await loadOlder(s, port, 1);
		expect(s.oldestId).toBe('b1');
		expect(port.fetchPage).toHaveBeenLastCalledWith('d1');

		await loadOlder(s, port, 1);
		expect(s.oldestId).toBe('c1');
		expect(port.fetchPage).toHaveBeenLastCalledWith('b1');
		expect(s.hasMore).toBe(false);
	});

	it('clears hasMore when the backend returns an empty page with has_more false', async () => {
		const el = makeScrollEl();
		const port: PagerPort = {
			fetchPage: async () => ({ messages: [], has_more: false, oldest_id: null }),
			prepend: vi.fn(),
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'x' });
		const n = await loadOlder(s, port, 1);
		expect(n).toBe(0);
		expect(s.hasMore).toBe(false);
		expect(port.prepend).not.toHaveBeenCalled();
	});

	it('advances cursor and continues when orphan-only window returns empty+has_more', async () => {
		const el = makeScrollEl({ perMsgPx: 20 });
		el._count = 10;
		el.scrollTop = 0;
		const realMsg = { id: 'anchor', content: 'the real message' };
		const fetchPage = vi
			.fn()
			.mockResolvedValueOnce({ messages: [], has_more: true, oldest_id: 'raw-boundary' })
			.mockResolvedValueOnce({ messages: [realMsg], has_more: false, oldest_id: 'anchor' });
		const prepend = vi.fn((older: any[]) => {
			el._count += older.length;
		});
		const port: PagerPort = {
			fetchPage,
			prepend,
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'first-cursor' });

		const n = await loadOlder(s, port, 4);
		expect(fetchPage).toHaveBeenCalledTimes(2);
		expect(fetchPage).toHaveBeenNthCalledWith(1, 'first-cursor');
		expect(fetchPage).toHaveBeenNthCalledWith(2, 'raw-boundary');
		expect(prepend).toHaveBeenCalledTimes(1);
		expect(n).toBe(1);
		expect(s.hasMore).toBe(false);
		expect(s.oldestId).toBe('anchor');
	});
});

describe('loadOlder — re-arm (the #165 fix)', () => {
	it('keeps filling while the restored position stays in the trigger band', async () => {
		const el = makeScrollEl({ perMsgPx: 5 });
		el._count = 2;
		el.scrollTop = 0;
		const pages = [
			pageOf(['p3', 'p4'], true),
			pageOf(['p1', 'p2'], true),
			pageOf(['p0'], false),
		];
		let i = 0;
		const port: PagerPort = {
			fetchPage: async () => pages[i++],
			prepend: (older) => {
				el._count += older.length;
				el.scrollTop = 0;
			},
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'p5' });

		const n = await loadOlder(s, port);
		expect(n).toBe(5);
		expect(s.hasMore).toBe(false);
		expect(s.oldestId).toBe('p0');
	});

	it('stops re-arming once the viewport clears the band', async () => {
		const el = makeScrollEl({ perMsgPx: 400 });
		el._count = 1;
		el.scrollTop = 0;
		const fetchPage = vi.fn(async () => pageOf(['q1', 'q2'], true));
		const port: PagerPort = {
			fetchPage,
			prepend: (older) => {
				el._count += older.length;
				el.scrollTop = 800;
			},
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'q0' });

		await loadOlder(s, port);
		expect(fetchPage).toHaveBeenCalledTimes(1);
	});

	it('keeps the scroll-only default at eight pages', async () => {
		const el = makeScrollEl({ perMsgPx: 1 });
		el.scrollTop = 0;
		const fetchPage = vi.fn(async () => pageOf(['scroll-only'], true));
		const port: PagerPort = {
			fetchPage,
			prepend: () => { el._count++; el.scrollTop = 0; },
			getScrollEl: () => el,
			afterRender: async () => {},
		};

		await loadOlder(createPager({ hasMore: true, oldestId: 'start' }), port);

		expect(fetchPage).toHaveBeenCalledTimes(8);
	});

	it('bounds an explicitly sized re-arm chain', async () => {
		const el = makeScrollEl({ perMsgPx: 1 });
		el.scrollTop = 0;
		const fetchPage = vi.fn(async () => pageOf(['z'], true));
		const port: PagerPort = {
			fetchPage,
			prepend: (older) => {
				el._count += older.length;
				el.scrollTop = 0;
			},
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'start' });
		await loadOlder(s, port, 3);
		expect(fetchPage).toHaveBeenCalledTimes(3);
	});
});

describe('loadOlder — Focus continuation', () => {
	it('chains past the scroll trigger while a visible entry is still absent', async () => {
		const el = makeScrollEl();
		el.scrollTop = 500;
		let visible = false;
		const fetchPage = vi.fn()
			.mockResolvedValueOnce(pageOf(['tool-only'], true))
			.mockResolvedValueOnce(pageOf(['reply'], true));
		const port: PagerPort = {
			fetchPage,
			prepend: (older) => { if (older[0].id === 'reply') visible = true; },
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const state = createPager({ hasMore: true, oldestId: 'tail' });

		await loadOlder(state, port, undefined, () => !visible);

		expect(fetchPage).toHaveBeenCalledTimes(2);
	});

	it('stops as soon as a visible entry appears', async () => {
		const el = makeScrollEl();
		el.scrollTop = 500;
		let visible = false;
		const fetchPage = vi.fn(async () => pageOf(['reply'], true));
		const port: PagerPort = {
			fetchPage,
			prepend: () => { visible = true; },
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const state = createPager({ hasMore: true, oldestId: 'tail' });

		await loadOlder(state, port, undefined, () => !visible);

		expect(fetchPage).toHaveBeenCalledOnce();
	});

	it('caps a continuation chain at 20 pages', async () => {
		const el = makeScrollEl();
		el.scrollTop = 500;
		const fetchPage = vi.fn(async () => pageOf(['tool-only'], true));
		const port: PagerPort = {
			fetchPage,
			prepend: vi.fn(),
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const state = createPager({ hasMore: true, oldestId: 'tail' });

		await loadOlder(state, port, undefined, () => true);

		expect(fetchPage).toHaveBeenCalledTimes(MAX_PAGER_CHAIN);
	});

	it('stops a continuation when history ends', async () => {
		const el = makeScrollEl();
		el.scrollTop = 500;
		const fetchPage = vi.fn(async () => pageOf(['tool-only'], false));
		const port: PagerPort = {
			fetchPage,
			prepend: vi.fn(),
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const state = createPager({ hasMore: true, oldestId: 'tail' });

		await loadOlder(state, port, undefined, () => true);

		expect(fetchPage).toHaveBeenCalledOnce();
	});

	it('keeps the non-Focus path to its existing scroll-only behavior', async () => {
		const el = makeScrollEl();
		el.scrollTop = 500;
		const fetchPage = vi.fn(async () => pageOf(['older'], true));
		const port: PagerPort = {
			fetchPage,
			prepend: vi.fn(),
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const state = createPager({ hasMore: true, oldestId: 'tail' });

		await loadOlder(state, port);

		expect(fetchPage).toHaveBeenCalledOnce();
	});
});

describe('loadOlder — guards', () => {
	it('is a no-op without a cursor or more pages', async () => {
		const port: PagerPort = {
			fetchPage: vi.fn(),
			prepend: vi.fn(),
			getScrollEl: () => null,
			afterRender: async () => {},
		};
		expect(await loadOlder(createPager({ hasMore: false }), port)).toBe(0);
		expect(await loadOlder(createPager({ hasMore: true, oldestId: null }), port)).toBe(0);
		expect(port.fetchPage).not.toHaveBeenCalled();
	});

	it('does not stack concurrent calls', async () => {
		const el = makeScrollEl();
		el.scrollTop = 500;
		let resolve!: (p: MessagePage) => void;
		const gate = new Promise<MessagePage>((r) => (resolve = r));
		const fetchPage = vi.fn(() => gate);
		const port: PagerPort = {
			fetchPage,
			prepend: (older) => {
				el._count += older.length;
			},
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'a' });

		const first = loadOlder(s, port, 1);
		const second = loadOlder(s, port, 1);
		resolve(pageOf(['x'], false));
		await Promise.all([first, second]);
		expect(fetchPage).toHaveBeenCalledOnce();
	});

	it('marks error and keeps the cursor on a failed fetch', async () => {
		const el = makeScrollEl();
		const port: PagerPort = {
			fetchPage: async () => null,
			prepend: vi.fn(),
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'keep-me' });
		const n = await loadOlder(s, port, 1);
		expect(n).toBe(0);
		expect(s.error).toBe(true);
		expect(s.hasMore).toBe(true);
		expect(s.oldestId).toBe('keep-me');
		expect(s.loading).toBe(false);
	});

	it('silently aborts on PAGER_ABORT without setting error', async () => {
		const el = makeScrollEl();
		const port: PagerPort = {
			fetchPage: async () => PAGER_ABORT,
			prepend: vi.fn(),
			getScrollEl: () => el,
			afterRender: async () => {},
		};
		const s = createPager({ hasMore: true, oldestId: 'stale-cursor' });
		const n = await loadOlder(s, port, 1);
		expect(n).toBe(0);
		expect(s.error).toBe(false);
		expect(s.hasMore).toBe(true);
		expect(s.loading).toBe(false);
	});
});
