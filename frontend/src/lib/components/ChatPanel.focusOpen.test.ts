import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPanel from './ChatPanel.svelte';
import { withEmptyAttentionQueue } from '../__tests__/fixtures/attentionQueue';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages, reasoningEffort, threads } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { _resetCheckpointsForTests } from '$lib/stores/checkpoints';
import { _resetSubagentHydrationForTests, subagentMeta } from '$lib/stores/subagents';
import { clearAllContextUsage } from '$lib/stores/contextUsageByThread';
import { coreSettings } from '$lib/stores/settings';

/** Opening a thread in Focus: one page, one stub per collapsed run, and a run's rows only when it is opened. */

const originalFetch = globalThis.fetch;
const row = (id: string, seq: number, extra: Record<string, unknown> = {}) => ({
	id, thread_id: 'thread-1', role: 'assistant', content: '', seq, created_at: '2026-10-02T10:00:00Z',
	activity_weight: 1, metadata: { turn_id: 't1' }, ...extra,
});
const said = (id: string, seq: number) => row(id, seq, {
	content: `said ${id}`, activity_weight: 0, metadata: { turn_id: 't1', for_user: true },
});
const stub = {
	kind: 'run', first_id: 'w2', last_id: 'w9', first_seq: 2, last_seq: 9, count: 8,
	turn_ids: ['t1'], weight: 8, continues_older: false,
};

let host: HTMLElement;
let component: Record<string, any> | null = null;
let urls: URL[];

function serve(opts: { olderHasMore: boolean }) {
	urls = [];
	globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
		const url = new URL(String(input), 'http://app.test');
		urls.push(url);
		const body = (value: unknown) => ({ ok: true, status: 200, json: async () => value });
		if (url.pathname.endsWith('/updates')) return body({ cards: [], has_more: false, divider: null });
		if (!url.pathname.endsWith('/messages')) return body({ thread_kind: 'chat' });
		if (url.searchParams.has('from_id')) {
			return body({ messages: [row('w8', 8), row('w9', 9)], has_more: false, oldest_id: 'w8' });
		}
		if (url.searchParams.has('before')) {
			return body({ messages: [said('older', 1)], runs: [], has_more: opts.olderHasMore, oldest_id: 'older' });
		}
		return body({ messages: [said('reply', 10)], runs: [stub], has_more: true, oldest_id: 'w2' });
	}) as unknown as typeof fetch;
}
const pageReads = () => urls.filter((url) => url.pathname.endsWith('/messages') && !url.searchParams.has('from_id'));

beforeEach(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(), cancel: vi.fn(), commitStyles: vi.fn(), onfinish: null, playState: 'finished',
		})) as unknown as Element['animate'];
	}
	vi.spyOn(console, 'error').mockImplementation(() => {});
	token.set('tok');
	threads.set([{ id: 'thread-1', title: 'One' } as never]);
	activeThreadId.set(null);
	messages.set([]);
	subagentMeta.set(new Map());
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	_resetSubagentHydrationForTests();
	clearAllContextUsage();
	reasoningEffort.set(null);
	coreSettings.set({ features: { focus_mode: true } } as never);
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	messages.set([]);
	coreSettings.set(null);
	_resetSubagentHydrationForTests();
	_resetCheckpointsForTests();
	globalThis.fetch = originalFetch;
	activeThreadId.set(null);
});

async function open() {
	globalThis.fetch = withEmptyAttentionQueue(globalThis.fetch);
	component = mount(ChatPanel, { target: host, props: { onOpenLightbox: () => {} } });
	flushSync();
	await component.selectThread('thread-1');
	flushSync();
}

describe('opening a thread in Focus', () => {
	it('reads the Focus view and fetches no hidden row, only a stub per collapsed run', async () => {
		serve({ olderHasMore: true });
		await open();
		await vi.waitFor(() => expect(host.querySelector('.fold-toggle')).not.toBeNull());

		expect(pageReads()[0].searchParams.get('view')).toBe('focus');
		expect(urls.some((url) => url.searchParams.has('from_id'))).toBe(false);
		expect(host.querySelector('.fold-toggle')?.textContent).toContain('Worked');
		expect([...document.querySelectorAll('.message')].map((node) => node.getAttribute('data-message-id')))
			.not.toContain('w9');
	});

	it('never chains pages: one request per scroll toward older history', async () => {
		serve({ olderHasMore: true });
		await open();
		await vi.waitFor(() => expect(pageReads().length).toBeGreaterThanOrEqual(2));
		await new Promise((resolve) => setTimeout(resolve, 50));

		// The first page, and the single older page the short transcript asked for.
		expect(pageReads()).toHaveLength(2);
		expect(pageReads()[1].searchParams.get('view')).toBe('focus');
	});

	it('fetches /updates once for the open, however many pages follow', async () => {
		serve({ olderHasMore: true });
		await open();
		await vi.waitFor(() => expect(pageReads()).toHaveLength(2));
		await new Promise((resolve) => setTimeout(resolve, 50));

		expect(urls.filter((url) => url.pathname.endsWith('/updates'))).toHaveLength(1);
	});

	it('reads a run\'s range when it is opened, and shows its rows', async () => {
		serve({ olderHasMore: false });
		await open();
		await vi.waitFor(() => expect(host.querySelector('.fold-toggle')).not.toBeNull());

		host.querySelector<HTMLButtonElement>('.fold-toggle')!.click();
		flushSync();

		await vi.waitFor(() => expect(urls.some((url) => url.searchParams.has('from_id'))).toBe(true));
		const range = urls.find((url) => url.searchParams.has('from_id'))!;
		expect(range.searchParams.get('from_id')).toBe('w2');
		expect(range.searchParams.get('to_id')).toBe('w9');
		await vi.waitFor(() => expect(host.querySelectorAll('.fold-details .message').length).toBe(2));
	});

	it('shows a failed range read inline and reads again on retry', async () => {
		serve({ olderHasMore: false });
		const base = globalThis.fetch;
		let failNext = true;
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('from_id') && failNext) {
				failNext = false;
				return { ok: false, status: 500, json: async () => ({}) };
			}
			return (base as typeof fetch)(input);
		}) as unknown as typeof fetch;
		await open();
		await vi.waitFor(() => expect(host.querySelector('.fold-toggle')).not.toBeNull());

		host.querySelector<HTMLButtonElement>('.fold-toggle')!.click();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.fold-error')).not.toBeNull());
		host.querySelector<HTMLButtonElement>('.fold-error .fold-action')!.click();

		await vi.waitFor(() => expect(host.querySelectorAll('.fold-details .message').length).toBe(2));
		expect(host.querySelector('.fold-error')).toBeNull();
	});
});

describe('opening a thread with Focus off', () => {
	it('does not ask for the Focus view', async () => {
		coreSettings.set({ features: { focus_mode: false } } as never);
		serve({ olderHasMore: false });
		await open();

		expect(pageReads().every((url) => !url.searchParams.has('view'))).toBe(true);
	});
});
