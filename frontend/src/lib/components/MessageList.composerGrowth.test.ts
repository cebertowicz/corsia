/**
 * A keystroke that grows the composer must settle the transcript ONCE.
 *
 * THE DEFECT THIS ENCODES (#3324). Typing made the chat canvas above the
 * composer lurch. Two owners settled the same keystroke: `InputBar.autoResize`
 * published `--input-bar-height` and then wrote `messagesEl.scrollTop` itself
 * after a tick, while `MessageList`'s ResizeObserver over `.input-floater`
 * re-pinned the live edge for the very same growth — and that direct write
 * bypassed `beginProgrammaticScroll`, so it could neither coalesce with the
 * owner's write nor respect a scroll the user had already started.
 *
 * Measured in a real browser before the fix (disposable worktree instance):
 * one composer growth produced a write from `InputBar.autoResize` and a second
 * from `beginProgrammaticScroll`, in different rendering updates. jsdom has no
 * layout, so this file asserts the write ownership instead of pixels: the
 * transcript's position has exactly one writer, and one keystroke settles it
 * once.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { activeThreadId, messages } from '$lib/stores/threads';
import { token } from '$lib/stores/auth';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import MessageList from '$lib/components/MessageList.svelte';
import InputBar from '$lib/components/InputBar.svelte';

const THREAD_ID = 'composer-growth-thread';
const VIEWPORT = 500;
const CONTENT = 5000;
/** The composer's reserved band while it is one line tall. */
const BAND = 72;
/** What a wrapped line adds to the band. */
const BAND_GROWTH = 24;

interface Panel {
	container: HTMLElement;
	scroller: HTMLElement;
	floater: HTMLElement;
	band: { height: number };
}

let host: HTMLElement;
let mounted: Record<string, unknown>[] = [];
let frames: FrameRequestCallback[] = [];
let realRaf: typeof requestAnimationFrame;
let realRO: typeof ResizeObserver;
let observers: { callback: ResizeObserverCallback; targets: Element[] }[] = [];

function mountPanel(): Panel {
	// The transcript's own container, exactly as the chat shell composes it:
	// `.messages` and `.input-floater` are siblings inside it.
	const container = document.createElement('div');
	container.className = 'chat-content';
	host.appendChild(container);

	const list = mount(MessageList, {
		target: container,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	const bar = mount(InputBar, {
		target: container,
		props: { onSend: () => {} },
	});

	const floater = container.querySelector<HTMLElement>('.input-floater')!;
	const band = { height: BAND };
	Object.defineProperty(floater, 'getBoundingClientRect', {
		configurable: true,
		value: () => ({ height: band.height }) as DOMRect,
	});
	flushSync();

	const scroller = container.querySelector<HTMLElement>('.messages')!;
	Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => CONTENT });
	Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => VIEWPORT });

	mounted.push(list, bar);
	return { container, scroller, floater, band };
}

function mountTranscriptWithoutComposer(): Pick<Panel, 'container' | 'scroller'> {
	const container = document.createElement('div');
	container.className = 'chat-content';
	host.appendChild(container);

	const list = mount(MessageList, {
		target: container,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	const scroller = container.querySelector<HTMLElement>('.messages')!;
	Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => CONTENT });
	Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => VIEWPORT });
	mounted.push(list);
	return { container, scroller };
}

/** Deliver the ResizeObserver callbacks a real layout change would deliver. */
function fireObservers(): void {
	for (const { callback, targets } of observers) {
		callback(
			targets.map((target) => ({ target }) as ResizeObserverEntry),
			{} as ResizeObserver,
		);
	}
	flushSync();
}

function runFrames(): void {
	const pending = frames;
	frames = [];
	for (const frame of pending) frame(0);
	flushSync();
}

/** Record every write to the scroller, however it is made. */
function recordScrollWrites(el: HTMLElement): number[] {
	const writes: number[] = [];
	let value = el.scrollTop;
	Object.defineProperty(el, 'scrollTop', {
		configurable: true,
		get: () => value,
		set: (next: number) => {
			value = next;
			writes.push(next);
		},
	});
	return writes;
}

/** A keystroke in the composer: `handleInput` → `autoResize`. */
function typeInto(container: HTMLElement, text: string): void {
	const composer = container.querySelector<HTMLElement>('.composer')!;
	composer.textContent = text;
	composer.dispatchEvent(new Event('input', { bubbles: true }));
}

/** The user scrolls away from the live edge, as a real gesture does. */
function userScrollsTo(el: HTMLElement, top: number): void {
	el.dispatchEvent(new Event('wheel'));
	el.scrollTop = top;
	el.dispatchEvent(new Event('scroll'));
	flushSync();
}

beforeEach(() => {
	realRaf = globalThis.requestAnimationFrame;
	frames = [];
	globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
		frames.push(cb);
		return frames.length;
	}) as typeof requestAnimationFrame;

	realRO = globalThis.ResizeObserver;
	observers = [];
	globalThis.ResizeObserver = class {
		constructor(callback: ResizeObserverCallback) {
			observers.push({ callback, targets: [] });
		}
		observe(target: Element) {
			observers.at(-1)!.targets.push(target);
		}
		disconnect() {}
	} as unknown as typeof ResizeObserver;

	host = document.createElement('div');
	document.body.appendChild(host);
	// The transcript reads its update cards on open; this file measures scroll
	// writes, so answer that read instead of letting it fail into the log.
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ cards: [], has_more: false, divider: null }) })));
	token.set('token');
	activeThreadId.set(THREAD_ID);
	messages.set([
		{
			id: 'm1', thread_id: THREAD_ID, role: 'assistant',
			content: 'a long answer', created_at: '2026-08-01T00:00:00Z',
		},
	]);
	_resetTurnStateForTests();
});

afterEach(() => {
	for (const component of mounted) unmount(component);
	mounted = [];
	host.remove();
	globalThis.requestAnimationFrame = realRaf;
	globalThis.ResizeObserver = realRO;
	activeThreadId.set(null);
	messages.set([]);
	_resetTurnStateForTests();
	vi.unstubAllGlobals();
});

describe('a keystroke that grows the composer', () => {
	it('settles the live-edge transcript exactly once', async () => {
		const panel = mountPanel();
		const writes = recordScrollWrites(panel.scroller);

		typeInto(panel.container, 'a line');
		await tick();
		flushSync();

		// The browser lays the taller composer out; the reserved band grows with it.
		panel.band.height = BAND + BAND_GROWTH;
		fireObservers();
		runFrames();

		expect(writes).toEqual([CONTENT]);
	});

	it('re-pins after the frame lays out, so a reservation published by a later observer is included', () => {
		// The attention slot publishes its reserved height from its own
		// ResizeObserver; a same-callback write pinned to the old bottom and left
		// the phone transcript's last row under the dock.
		const panel = mountPanel();
		const writes = recordScrollWrites(panel.scroller);

		panel.band.height = BAND + BAND_GROWTH;
		fireObservers();
		expect(writes).toEqual([]);

		runFrames();
		expect(writes).toEqual([CONTENT]);
	});

	it('writes nothing while the user is reading history', async () => {
		const panel = mountPanel();
		userScrollsTo(panel.scroller, 0);
		const writes = recordScrollWrites(panel.scroller);

		typeInto(panel.container, 'a line');
		await tick();
		flushSync();

		panel.band.height = BAND + BAND_GROWTH;
		fireObservers();
		runFrames();

		expect(writes).toEqual([]);
	});
});

describe('two transcripts beside each other (#1949)', () => {
	it('does not fall back to a neighboring composer when its own container has none', () => {
		const transcript = mountTranscriptWithoutComposer();
		const neighboringPanel = mountPanel();
		const transcriptWrites = recordScrollWrites(transcript.scroller);
		const neighboringWrites = recordScrollWrites(neighboringPanel.scroller);

		neighboringPanel.band.height = BAND + BAND_GROWTH;
		fireObservers();
		runFrames();

		expect(transcriptWrites).toEqual([]);
		expect(neighboringWrites).toEqual([CONTENT]);
	});

	it('re-pins only the transcript whose own composer grew', () => {
		const chat = mountPanel();
		const leaf = mountPanel();
		const chatWrites = recordScrollWrites(chat.scroller);
		const leafWrites = recordScrollWrites(leaf.scroller);

		// Only the leaf's composer grows.
		leaf.band.height = BAND + BAND_GROWTH;
		fireObservers();
		runFrames();

		expect(chatWrites).toEqual([]);
		expect(leafWrites).toEqual([CONTENT]);
	});
});
