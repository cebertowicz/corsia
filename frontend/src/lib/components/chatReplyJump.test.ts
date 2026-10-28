import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { activeThreadId, messages } from '$lib/stores/threads';
import { createPager } from '$lib/messagePager';
import { createChatReplyJumper } from './chatReplyJump';
import type { RunStub } from '$lib/focusRuns';

const destination = (id: string) => ({ pane: 'chat-replies', id, seq: 5, cid: 'c1' });
const row = (id: string, thread = 't1') => ({ id, thread_id: thread, seq: 5 } as never);
let element: HTMLElement;

beforeEach(() => {
	activeThreadId.set('t1');
	messages.set([]);
	element = document.createElement('div');
	element.innerHTML = '<div data-message-id="a"><div data-chat-comment="c1"></div></div><div data-message-id="b"><div data-chat-comment="c1"></div></div>';
	document.body.append(element);
	vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => { queueMicrotask(() => fn(0)); return 1; });
	vi.stubGlobal('matchMedia', () => ({ matches: false }));
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { element.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function setup(overrides: { runs?: RunStub[]; loadRun?: (run: RunStub) => Promise<void> } = {}) {
	const pager = createPager({ hasMore: true, oldestId: 'old' });
	const loadOlder = vi.fn(async () => { messages.set([row('a')]); pager.oldestId = 'a'; });
	const loadRun = vi.fn(overrides.loadRun ?? (async () => {}));
	let generation = 1;
	const jumper = createChatReplyJumper({
		pager: () => pager, runs: () => overrides.runs ?? [], loads: () => ({}),
		loadOlder, loadRun, element: () => element, generation: () => generation,
	});
	return { pager, jumper, loadOlder, loadRun, invalidate: () => { generation++; } };
}

describe('reply navigation while the pager settles', () => {
	it('waits for an in-flight page, then continues the existing page walk', async () => {
		const { pager, jumper, loadOlder } = setup();
		pager.loading = true;
		jumper.jump(destination('a'));
		expect(loadOlder).not.toHaveBeenCalled();
		pager.loading = false;
		jumper.resume();
		await vi.waitFor(() => expect(element.querySelector('[data-chat-comment]')?.classList.contains('chat-reply-landed')).toBe(true));
		expect(loadOlder).toHaveBeenCalledTimes(1);
	});

	it('keeps the most recent click through an error until the existing retry succeeds', async () => {
		const { pager, jumper, loadOlder } = setup();
		pager.error = true;
		jumper.jump(destination('a'));
		jumper.jump(destination('b'));
		pager.loading = true;
		pager.error = false;
		jumper.resume();
		expect(loadOlder).not.toHaveBeenCalled();
		messages.set([row('b')]);
		pager.loading = false;
		jumper.resume();
		await vi.waitFor(() => expect(element.querySelector('[data-message-id="b"] [data-chat-comment]')?.classList.contains('chat-reply-landed')).toBe(true));
		expect(element.querySelector('[data-message-id="a"] [data-chat-comment]')?.classList.contains('chat-reply-landed')).toBe(false);
	});

	it('discards a queued click when the thread generation changes', async () => {
		const { pager, jumper, loadOlder, invalidate } = setup();
		pager.loading = true;
		jumper.jump(destination('a'));
		activeThreadId.set('t2');
		invalidate();
		activeThreadId.set('t1');
		pager.loading = false;
		jumper.resume();
		await Promise.resolve();
		expect(loadOlder).not.toHaveBeenCalled();
	});

	it('retains a click when its own page read fails, and resumes after retry', async () => {
		const { pager, jumper, loadOlder } = setup();
		loadOlder.mockImplementationOnce(async () => { pager.error = true; });
		jumper.jump(destination('a'));
		await vi.waitFor(() => expect(pager.error).toBe(true));
		pager.error = false;
		messages.set([row('a')]);
		jumper.resume();
		await vi.waitFor(() => expect(element.querySelector('[data-chat-comment]')?.classList.contains('chat-reply-landed')).toBe(true));
		expect(loadOlder).toHaveBeenCalledTimes(1);
	});

	it('continues through the Focus run loader after pager recovery', async () => {
		const stub = { firstSeq: 1, lastSeq: 9, firstId: 'first', lastId: 'last' } as RunStub;
		const { pager, jumper, loadRun, loadOlder } = setup({ runs: [stub], loadRun: async () => { messages.set([row('a')]); } });
		pager.loading = true;
		jumper.jump(destination('a'));
		pager.loading = false;
		jumper.resume();
		await vi.waitFor(() => expect(loadRun).toHaveBeenCalledTimes(1));
		await vi.waitFor(() => expect(element.querySelector('[data-chat-comment]')?.classList.contains('chat-reply-landed')).toBe(true));
		expect(loadOlder).not.toHaveBeenCalled();
	});
});
