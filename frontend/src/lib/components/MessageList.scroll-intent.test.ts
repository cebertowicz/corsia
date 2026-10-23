import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { _resetTurnStateForTests, INITIAL_TURN_STATE, restoreTurnState } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

/**
 * The transcript must never fight the user's finger.
 *
 * `userIsNearBottom` is a bindable, so these assert the user-visible proxy for
 * it instead: the scroll-to-bottom FAB renders exactly when the transcript
 * believes the user has scrolled away from the live edge.
 */

const THREAD_ID = 'scroll-intent-session';
const VIEWPORT = 500;
const CONTENT = 5000;

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let frames: FrameRequestCallback[] = [];
let realRaf: typeof requestAnimationFrame;

function scroller(): HTMLElement {
	const el = host.querySelector<HTMLElement>('.messages');
	expect(el).not.toBeNull();
	return el!;
}

function followingLiveEdge(): boolean {
	return host.querySelector('.scroll-fab') === null;
}

function runFrames(): void {
	const pending = frames;
	frames = [];
	for (const frame of pending) frame(0);
	flushSync();
}

function measure(el: HTMLElement): void {
	Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => CONTENT });
	Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => VIEWPORT });
}

function userScrollsTo(el: HTMLElement, top: number): void {
	el.dispatchEvent(new Event('wheel'));
	el.scrollTop = top;
	el.dispatchEvent(new Event('scroll'));
	flushSync();
}

function renderList(): Record<string, any> {
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
	measure(scroller());
	return component as Record<string, any>;
}

beforeEach(() => {
	realRaf = globalThis.requestAnimationFrame;
	frames = [];
	globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
		frames.push(cb);
		return frames.length;
	}) as typeof requestAnimationFrame;
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	messages.set([
		{
			id: 'm1', thread_id: THREAD_ID, role: 'assistant',
			content: 'a long answer', created_at: '2026-08-01T00:00:00Z',
		},
	] as never);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.requestAnimationFrame = realRaf;
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	messages.set([]);
	_resetTurnStateForTests();
});

describe('a deliberate user scroll always wins', () => {
	it('does not discard a user scroll that lands inside the programmatic window', async () => {
		const list = renderList();
		list.scrollToBottom();
		await tick();
		flushSync();

		// The programmatic write has landed; its release frame has NOT run yet.
		// This is exactly the window in which a finger drag used to be thrown away.
		userScrollsTo(scroller(), 0);

		expect(followingLiveEdge()).toBe(false);
	});

	it('does not overwrite a registered user scroll when the pending frame fires', async () => {
		const list = renderList();
		list.scrollToBottom();
		await tick();
		flushSync();
		userScrollsTo(scroller(), 0);

		runFrames();

		expect(followingLiveEdge()).toBe(false);
	});

	it('abandons a pin that the user overrode before it could land', async () => {
		const list = renderList();
		const el = scroller();

		// A stream event asks to pin to the live edge; the write is deferred one
		// tick so the new row can lay out. The finger arrives inside that gap.
		list.scrollToBottom();
		userScrollsTo(el, 1200);
		await tick();
		flushSync();
		runFrames();

		expect(el.scrollTop).toBe(1200);
		expect(followingLiveEdge()).toBe(false);
	});

	it('keeps following the live edge when the user does not intervene', async () => {
		const list = renderList();
		const el = scroller();
		el.scrollTop = 0;
		el.dispatchEvent(new Event('scroll'));
		flushSync();
		expect(followingLiveEdge()).toBe(false);

		list.scrollToBottom();
		await tick();
		flushSync();
		runFrames();

		expect(followingLiveEdge()).toBe(true);
	});

	it('re-follows once the user scrolls back down to the live edge', async () => {
		renderList();
		const el = scroller();
		userScrollsTo(el, 0);
		expect(followingLiveEdge()).toBe(false);

		userScrollsTo(el, CONTENT - VIEWPORT);

		expect(followingLiveEdge()).toBe(true);
	});
});

describe('the first-run opening live edge', () => {
	it.each([
		{ label: 'queued delivery', pending: { id: 'queued', delivery_state: 'queued' } },
		{ label: 'optimistic delivery during an active turn', pending: { id: 'optimistic', _optimistic: true }, activeTurn: true },
	])('follows a $label below a revealed opening instead of re-anchoring the greeting', async ({ pending, activeTurn }) => {
		if (activeTurn) restoreTurnState(THREAD_ID, {
			...INITIAL_TURN_STATE, phase: 'active', turnId: 'turn', turnOrder: 1,
		});
		messages.set([
			{ id: 'opening', thread_id: THREAD_ID, role: 'assistant', content: 'Hi.', seq: 1,
				created_at: '2026-08-01T00:00:00Z', metadata: { first_run_opening: true } },
			{ ...pending, thread_id: THREAD_ID, role: 'user', content: 'Pending input',
				created_at: '2026-08-01T00:01:00Z' },
		] as never);
		const list = renderList();
		const el = scroller();
		el.querySelector<HTMLElement>('[data-message-id="opening"]')!.scrollIntoView = () => {};

		list.followBottom();
		await tick();
		flushSync();

		expect(el.scrollTop).toBe(CONTENT);
	});
});

describe('the shared bottom action', () => {
	it('keeps its bottom contract when the opening is the final transcript row', async () => {
		messages.set([{ id: 'opening', thread_id: THREAD_ID, role: 'assistant', content: 'Hi.', seq: 1,
			created_at: '2026-08-01T00:00:00Z', metadata: { first_run_opening: true } }] as never);
		const list = renderList();
		const el = scroller();
		el.querySelector<HTMLElement>('[data-message-id="opening"]')!.scrollIntoView = () => {};

		list.scrollToBottomFromUser();
		await tick();
		flushSync();
		runFrames();

		expect(el.scrollTop).toBe(CONTENT);
		expect(followingLiveEdge()).toBe(true);
	});
});

describe('the composer growing is not a user scroll', () => {
	/**
	 * Reported as "queued messages hide the typing indicator", and reproduced on
	 * dev: queue one message, the chip appears, `--input-bar-height` grows, this
	 * list's bottom padding grows with it — and the live edge slides out of the
	 * viewport carrying the thinking indicator with it. The user never touched
	 * the scroll.
	 *
	 * Asserted on the re-pin ITSELF — a recorded write to `scrollTop` — rather
	 * than on the scroll-to-bottom FAB. jsdom has no layout, so the FAB proxy
	 * cannot move here and a cell written against it passes whether or not the
	 * fix exists (checked: it did).
	 */
	function installFloater(height: number): HTMLElement {
		const floater = document.createElement('div');
		floater.className = 'input-floater';
		Object.defineProperty(floater, 'getBoundingClientRect', {
			configurable: true, value: () => ({ height }) as DOMRect,
		});
		host.appendChild(floater);
		return floater;
	}

	function recordScrollWrites(el: HTMLElement): number[] {
		const writes: number[] = [];
		let value = 0;
		Object.defineProperty(el, 'scrollTop', {
			configurable: true,
			get: () => value,
			set: (next: number) => { value = next; writes.push(next); },
		});
		return writes;
	}

	function mountWithFloater(): { list: Record<string, any>; fire: () => void } {
		let observerCallback: ResizeObserverCallback | null = null;
		const observed: Element[] = [];
		globalThis.ResizeObserver = class {
			constructor(cb: ResizeObserverCallback) { observerCallback = cb; }
			observe(element: Element) { observed.push(element); }
			disconnect() {}
		} as unknown as typeof ResizeObserver;
		const list = renderList();
		return {
			list,
			fire: () => observerCallback?.(
				observed.map((target) => ({ target }) as ResizeObserverEntry),
				{} as ResizeObserver,
			),
		};
	}

	function installAttentionRail(height: number): HTMLElement {
		const rail = document.createElement('div');
		rail.className = 'chat-attention-stack';
		Object.defineProperty(rail, 'getBoundingClientRect', {
			configurable: true, value: () => ({ height }) as DOMRect,
		});
		host.appendChild(rail);
		return rail;
	}

	let realRO: typeof ResizeObserver;
	let floater: HTMLElement;
	let attentionRail: HTMLElement;

	beforeEach(() => { realRO = globalThis.ResizeObserver; });
	afterEach(() => {
		globalThis.ResizeObserver = realRO;
		floater?.remove();
		attentionRail?.remove();
	});

	it('re-pins the live edge when the queued-chip region appears', async () => {
		floater = installFloater(72);
		const { fire } = mountWithFloater();
		const writes = recordScrollWrites(scroller());

		// The chip lands: the floater is taller, so the reserved padding grows.
		Object.defineProperty(floater, 'getBoundingClientRect', {
			configurable: true, value: () => ({ height: 110 }) as DOMRect,
		});
		fire();
		runFrames();

		expect(writes.length).toBeGreaterThan(0);
		expect(writes.at(-1)).toBe(CONTENT);
	});

	it('keeps a reader at the bottom when the composer grows below a long opening', async () => {
		messages.set([{ id: 'opening', thread_id: THREAD_ID, role: 'assistant', content: 'Hi.', seq: 1,
			created_at: '2026-08-01T00:00:00Z', metadata: { first_run_opening: true } }] as never);
		floater = installFloater(72);
		const { fire } = mountWithFloater();
		const el = scroller();
		el.querySelector<HTMLElement>('[data-message-id="opening"]')!.scrollIntoView = () => {};
		userScrollsTo(el, CONTENT - VIEWPORT);
		const writes = recordScrollWrites(el);

		Object.defineProperty(floater, 'getBoundingClientRect', {
			configurable: true, value: () => ({ height: 110 }) as DOMRect,
		});
		fire();
		runFrames();

		expect(writes.at(-1)).toBe(CONTENT);
	});

	it('leaves a user who scrolled up exactly where they are', async () => {
		floater = installFloater(72);
		const { fire } = mountWithFloater();
		userScrollsTo(scroller(), 0);
		const writes = recordScrollWrites(scroller());

		Object.defineProperty(floater, 'getBoundingClientRect', {
			configurable: true, value: () => ({ height: 110 }) as DOMRect,
		});
		fire();
		runFrames();

		expect(writes).toEqual([]);
	});

	it('re-pins only a live transcript when the measured action rail grows', () => {
		attentionRail = installAttentionRail(44);
		const { fire } = mountWithFloater();
		const writes = recordScrollWrites(scroller());

		Object.defineProperty(attentionRail, 'getBoundingClientRect', {
			configurable: true, value: () => ({ height: 96 }) as DOMRect,
		});
		fire();
		runFrames();

		expect(writes.at(-1)).toBe(CONTENT);
	});
});
