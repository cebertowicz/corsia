import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import SurfaceThreadHarness from '$lib/__tests__/fixtures/SurfaceThreadHarness.svelte';
import type { CommentThreadData } from './CommentThreadData';

// Each thread card that opens at its newest reply writes its scroller's scrollTop once; this records the card each time.
const scrolled = vi.hoisted(() => [] as HTMLElement[]);
vi.mock('$lib/actions/keepInView', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/actions/keepInView')>()),
	scroller: (card: HTMLElement) => {
		const port = document.createElement('div');
		Object.defineProperty(port, 'scrollHeight', { value: 400 });
		Object.defineProperty(port, 'clientHeight', { value: 100 });
		Object.defineProperty(port, 'scrollTop', { get: () => 0, set: () => { if (card.matches('.ct')) scrolled.push(card); } });
		return port;
	},
}));

const reply = (n: number) => ({ id: `r${n}`, author: 'agent-1', authorKind: 'agent' as const, text: `Reply ${n}`, at: 'later' });
function thread(id: string, replies: number[], extra: Partial<CommentThreadData> = {}): CommentThreadData {
	return { id, anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: replies.map(reply), ...extra };
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let frames: FrameRequestCallback[] = [];
const instant = Element.prototype.animate;

/** Runs the frames asked for so far, and those they ask for, a few rounds deep. */
function runFrames(): void {
	for (let round = 0; round < 4; round++) {
		const due = frames; frames = [];
		due.forEach((frame) => frame(performance.now()));
		flushSync();
	}
}
async function settle(ms: number): Promise<void> {
	flushSync();
	await vi.advanceTimersByTimeAsync(ms);
	flushSync();
	runFrames();
}
const cards = () => [...document.querySelectorAll<HTMLElement>('.ct')];

beforeEach(() => {
	scrolled.length = 0;
	frames = [];
	vi.stubGlobal('requestAnimationFrame', (frame: FrameRequestCallback) => frames.push(frame));
	vi.stubGlobal('cancelAnimationFrame', () => {});
	// jsdom's stand-in animations end at once; these end after their duration, and a cancelled one never ends, so a
	// surface reopened during its close resumes the same content as in a browser.
	Element.prototype.animate = function animate(_keyframes, options) {
		const duration = typeof options === 'number' ? options : Number(options?.duration ?? 0);
		let timer: ReturnType<typeof setTimeout> | undefined;
		const animation = {
			cancel() { clearTimeout(timer); }, finish() {}, play() {}, pause() {}, reverse() {},
			addEventListener() {}, removeEventListener() {}, currentTime: 0, playState: 'running',
		} as unknown as Animation;
		Object.defineProperty(animation, 'onfinish', {
			set(callback: ((event: Event) => void) | null) {
				clearTimeout(timer);
				if (callback) timer = setTimeout(() => callback(new Event('finish')), duration);
			},
		});
		return animation;
	};
	document.documentElement.style.setProperty('--dur-tight', '120ms');
	vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
	host = document.createElement('div'); document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.useRealTimers();
	vi.unstubAllGlobals();
	Element.prototype.animate = instant;
	document.documentElement.style.removeProperty('--dur-tight');
});

describe('a thread opened on its own opens at its newest reply once per open', () => {
	// The sheet closes over 260 ms and a popover over 84 ms (0.7 of 120 ms): a reopen 40 ms in keeps the same card.
	it.each([['sheet', 'drawer'], ['popover', 'popover']] as const)('re-scrolls a %s reopened while it is still closing', async (_label, surface) => {
		const props = $state({ surface, open: true, comments: [thread('c1', [1, 2])] });
		component = mount(SurfaceThreadHarness, { target: host, props });
		await settle(400);
		const [card] = cards();
		expect(scrolled).toEqual([card]);

		props.open = false;
		await settle(40);
		expect(card.isConnected).toBe(true);
		props.open = true;
		await settle(400);

		expect(cards()).toEqual([card]);
		expect(scrolled).toEqual([card, card]);
	});

	it('does not scroll again for a new reply, or a re-render, while it stays open', async () => {
		const props = $state({ surface: 'drawer' as const, open: true, comments: [thread('c1', [1, 2])] });
		component = mount(SurfaceThreadHarness, { target: host, props });
		await settle(400);
		expect(scrolled).toHaveLength(1);

		props.comments = [thread('c1', [1, 2, 3])];
		await settle(400);
		expect(cards()[0].textContent).toContain('Reply 3');
		expect(scrolled).toHaveLength(1);

		props.comments = [thread('c1', [1, 2, 3], { text: 'note, edited' })];
		await settle(400);
		expect(cards()[0].textContent).toContain('note, edited');
		expect(scrolled).toHaveLength(1);
	});

	it('scrolls each resolved card in a sheet once per open of the sheet, and not for a new reply', async () => {
		const resolved = (id: string, replies: number[]) => thread(id, replies, { resolved: true, newReplyIds: [`r${replies.at(-1)}`] });
		const props = $state({ surface: 'drawer' as const, open: true, comments: [resolved('c1', [1, 2]), resolved('c2', [1])] });
		component = mount(SurfaceThreadHarness, { target: host, props });
		await settle(400);
		const [first, second] = cards();
		expect(new Set(scrolled)).toEqual(new Set([first, second]));
		expect(scrolled).toHaveLength(2);

		props.open = false;
		await settle(40);
		props.open = true;
		await settle(400);
		expect(cards()).toEqual([first, second]);
		expect(scrolled.filter((card) => card === first)).toHaveLength(2);
		expect(scrolled.filter((card) => card === second)).toHaveLength(2);

		props.comments = [resolved('c1', [1, 2, 3]), resolved('c2', [1])];
		await settle(400);
		expect(scrolled).toHaveLength(4);
	});
});
