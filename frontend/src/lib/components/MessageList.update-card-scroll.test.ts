import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { openAtDivider, threadUpdateCards, visitDividers } from '$lib/stores/threadUpdateCards';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

const THREAD_ID = 'update-card-scroll-session';
const CONTENT = 5000;
const VIEWPORT = 500;
const DIVIDER_DESTINATION = 1600;
const card = {
	id: 1,
	thread_id: THREAD_ID,
	kind: 'done' as const,
	node_id: 'node-1',
	title: 'Finished work',
	after_seq: 0,
	created_at: '2026-09-29T12:00:00Z',
	node_exists: true, asks_user: true, blocked_asks_user: false,
};

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let frames: FrameRequestCallback[] = [];
let realRaf: typeof requestAnimationFrame;

function scroller(): HTMLElement {
	const element = host.querySelector<HTMLElement>('.messages');
	expect(element).not.toBeNull();
	return element!;
}

function recordScrollWrites(element: HTMLElement): number[] {
	const writes: number[] = [];
	let value = element.scrollTop;
	Object.defineProperty(element, 'scrollTop', {
		configurable: true,
		get: () => value,
		set: (next: number) => { value = next; writes.push(next); },
	});
	return writes;
}

function renderList(): Record<string, unknown> {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	flushSync();
	Object.defineProperty(scroller(), 'scrollHeight', { configurable: true, get: () => CONTENT });
	Object.defineProperty(scroller(), 'clientHeight', { configurable: true, get: () => VIEWPORT });
	return component!;
}

function userScrollsTo(element: HTMLElement, top: number): void {
	element.dispatchEvent(new Event('wheel'));
	element.scrollTop = top;
	element.dispatchEvent(new Event('scroll'));
	flushSync();
}

function userScrollsUp(element: HTMLElement): void {
	userScrollsTo(element, 0);
}

function dividerWritesTo(element: HTMLElement, top = DIVIDER_DESTINATION): void {
	const divider = element.querySelector<HTMLElement>('[data-visit-divider]');
	expect(divider).not.toBeNull();
	divider!.scrollIntoView = () => { element.scrollTop = top; };
}

function runFrames(): void {
	const pending = frames;
	frames = [];
	for (const frame of pending) frame(0);
	flushSync();
}

beforeEach(() => {
	realRaf = globalThis.requestAnimationFrame;
	frames = [];
	globalThis.requestAnimationFrame = ((callback: FrameRequestCallback) => {
		frames.push(callback);
		return frames.length;
	}) as typeof requestAnimationFrame;
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	activeToolsByThread.set(new Map());
	openAtDivider.set(null);
	threadUpdateCards.set({});
	visitDividers.set({});
	_resetTurnStateForTests();
	messages.set([{
		id: 'm1', thread_id: THREAD_ID, role: 'assistant',
		content: 'a long answer', created_at: '2026-08-01T00:00:00Z',
	}] as never);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.requestAnimationFrame = realRaf;
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	openAtDivider.set(null);
	threadUpdateCards.set({});
	visitDividers.set({});
	messages.set([]);
	_resetTurnStateForTests();
});

describe('update cards preserve scroll intent', () => {
	it('re-pins an already pinned transcript when cards arrive', async () => {
		renderList();
		const writes = recordScrollWrites(scroller());

		threadUpdateCards.set({ [THREAD_ID]: { fromSeq: 0, cards: [card] } });
		flushSync();
		await tick();
		flushSync();
		runFrames();

		expect(writes.at(-1)).toBe(CONTENT);
	});

	it('leaves a user reading history exactly where they are', async () => {
		renderList();
		userScrollsUp(scroller());
		const writes = recordScrollWrites(scroller());

		threadUpdateCards.set({ [THREAD_ID]: { fromSeq: 0, cards: [card] } });
		flushSync();
		await tick();
		flushSync();
		runFrames();

		expect(writes).toEqual([]);
	});

	it('treats a newly selected row as a live-edge opening', async () => {
		renderList();
		userScrollsUp(scroller());
		activeThreadId.set('next-thread');
		messages.set([{
			id: 'm2', thread_id: 'next-thread', role: 'assistant',
			content: 'the next answer', created_at: '2026-09-29T12:05:00Z',
		}] as never);
		flushSync();
		const writes = recordScrollWrites(scroller());

		threadUpdateCards.set({
			'next-thread': { fromSeq: 0, cards: [{ ...card, thread_id: 'next-thread' }] },
		});
		flushSync();
		await tick();
		flushSync();
		runFrames();

		expect(writes.at(-1)).toBe(CONTENT);
	});

	it('keeps update-dot landing at the divider through delayed cards and live transcript pins', async () => {
		const list = renderList() as Record<string, () => void>;
		const element = scroller();
		const writes = recordScrollWrites(element);

		openAtDivider.set(THREAD_ID);
		visitDividers.set({ [THREAD_ID]: { divider: { since: '2026-09-29T11:00:00Z', first_seq: null }, failed: false } });
		threadUpdateCards.set({ [THREAD_ID]: { fromSeq: 0, cards: [card] } });
		flushSync();
		dividerWritesTo(element);
		await tick();
		flushSync();

		// The outer landing frame writes the frozen divider and queues the
		// programmatic release frame. Both cards and transcript events can arrive
		// in the interval after this write, so neither may re-pin the live edge.
		runFrames();
		expect(writes).toEqual([DIVIDER_DESTINATION]);
		threadUpdateCards.set({
			[THREAD_ID]: { fromSeq: 0, cards: [card, { ...card, id: 2, title: 'Another update' }] },
		});
		flushSync();
		list.scrollToBottom();
		list.followBottom();
		await tick();
		flushSync();

		expect(writes).toEqual([DIVIDER_DESTINATION]);
		runFrames();
		expect(writes.at(-1)).toBe(DIVIDER_DESTINATION);
	});

	it('cancels update-dot intent when a user gesture arrives before cards resolve', async () => {
		renderList();
		const element = scroller();
		const writes = recordScrollWrites(element);

		openAtDivider.set(THREAD_ID);
		flushSync();
		userScrollsTo(element, 1200);
		visitDividers.set({ [THREAD_ID]: { divider: { since: '2026-09-29T11:00:00Z', first_seq: null }, failed: false } });
		threadUpdateCards.set({ [THREAD_ID]: { fromSeq: 0, cards: [card] } });
		flushSync();
		dividerWritesTo(element);
		await tick();
		flushSync();
		runFrames();

		expect(writes).toEqual([1200]);
		expect(element.scrollTop).toBe(1200);
	});

	it('lets a user gesture cancel a pending update-dot landing', async () => {
		renderList();
		const element = scroller();
		const writes = recordScrollWrites(element);

		openAtDivider.set(THREAD_ID);
		visitDividers.set({ [THREAD_ID]: { divider: { since: '2026-09-29T11:00:00Z', first_seq: null }, failed: false } });
		threadUpdateCards.set({ [THREAD_ID]: { fromSeq: 0, cards: [card] } });
		flushSync();
		dividerWritesTo(element);
		await tick();
		flushSync();

		userScrollsTo(element, 1200);
		runFrames();

		expect(writes).toEqual([1200]);
		expect(element.scrollTop).toBe(1200);
	});

	it('cancels a pending update-dot landing when the thread changes', async () => {
		renderList();
		const element = scroller();
		const writes = recordScrollWrites(element);

		openAtDivider.set(THREAD_ID);
		visitDividers.set({ [THREAD_ID]: { divider: { since: '2026-09-29T11:00:00Z', first_seq: null }, failed: false } });
		threadUpdateCards.set({ [THREAD_ID]: { fromSeq: 0, cards: [card] } });
		flushSync();
		dividerWritesTo(element);
		await tick();
		flushSync();

		activeThreadId.set('next-thread');
		messages.set([{
			id: 'm2', thread_id: 'next-thread', role: 'assistant',
			content: 'the next answer', created_at: '2026-09-29T12:05:00Z',
		}] as never);
		flushSync();
		runFrames();

		expect(writes).toEqual([]);
	});
});
