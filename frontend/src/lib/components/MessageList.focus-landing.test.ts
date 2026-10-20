import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { coreSettings } from '$lib/stores/settings';
import { activeThreadId, activeToolsByThread, messages, type Message } from '$lib/stores/threads';
import { openAtDivider, threadUpdateCards, visitDividers } from '$lib/stores/threadUpdateCards';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { said, THREAD, work } from '$lib/__tests__/fixtures/focusRuns';
import { auto, compact } from '$lib/__tests__/fixtures/focusPauseProof';
import MessageList from './MessageList.svelte';
import messageListSource from './MessageList.svelte?raw';
import workBlockSource from './WorkBlock.svelte?raw';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let frames: FrameRequestCallback[] = [];
let realRaf: typeof requestAnimationFrame;

const scroller = () => host.querySelector<HTMLElement>('.messages')!;
const runFrames = () => { const pending = frames; frames = []; for (const frame of pending) frame(0); flushSync(); };
const rows = (...list: Record<string, any>[]) => list as unknown as Message[];
const divider = (first_seq: number | null) => ({ divider: { since: '2026-10-02T09:00:00Z', first_seq }, failed: false });

function render(): void {
	component = mount(MessageList, { target: host, props: { messagesEl: undefined, userIsNearBottom: true, ignoreScrollEvents: false, onOpenLightbox: () => {} } });
	flushSync();
}

/** Record where landing scrolls to: the element `scrollIntoView` is called on. */
function recordLanding(): Element[] {
	const landed: Element[] = [];
	Element.prototype.scrollIntoView = function (this: Element) { landed.push(this); };
	return landed;
}

beforeEach(() => {
	realRaf = globalThis.requestAnimationFrame;
	frames = [];
	globalThis.requestAnimationFrame = ((callback: FrameRequestCallback) => { frames.push(callback); return frames.length; }) as typeof requestAnimationFrame;
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD);
	activeToolsByThread.set(new Map());
	openAtDivider.set(null);
	threadUpdateCards.set({});
	visitDividers.set({});
	_resetTurnStateForTests();
	coreSettings.set({ features: { focus_mode: true } } as never);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.requestAnimationFrame = realRaf;
	activeThreadId.set(null);
	openAtDivider.set(null);
	threadUpdateCards.set({});
	visitDividers.set({});
	messages.set([]);
	coreSettings.set(null);
	_resetTurnStateForTests();
});

describe('F2 the block that holds what is new', () => {
	it('attaches the visit divider to the collapsed block and keeps the spoken new state', () => {
		messages.set(rows(work('w1', 1, '10:00:00'), work('w2', 2, '10:01:00'), said('s3', 3, '10:02:00')));
		visitDividers.set({ [THREAD]: divider(2) });
		render();
		const capsule = host.querySelector<HTMLElement>('[data-work-block][data-new]');
		expect(capsule).not.toBeNull();
		expect(capsule!.querySelector('[data-new-capsule]')).toBeNull();
		expect(capsule!.querySelector('[data-visit-divider]')).not.toBeNull();
		expect(capsule!.querySelector('.fold-toggle')?.getAttribute('aria-label')).toMatch(/^Expand .*, new since your last visit$/);
		expect(host.querySelectorAll('[data-work-block]')).toHaveLength(1);
	});

	it('puts the line inside the open block, before the first new row', () => {
		messages.set(rows(work('w1', 1, '10:00:00'), work('w2', 2, '10:01:00'), said('s3', 3, '10:02:00')));
		visitDividers.set({ [THREAD]: divider(2) });
		render();
		host.querySelector<HTMLButtonElement>('.fold-toggle')!.click();
		flushSync();
		const inside = host.querySelector('.fold-details [data-visit-divider]');
		expect(inside).not.toBeNull();
		expect(inside!.previousElementSibling?.getAttribute('data-message-id')).toBe('w1');
		expect(inside!.nextElementSibling?.getAttribute('data-message-id')).toBe('w2');
	});
});

describe('F7-F11 landing', () => {
	it('lands the attached divider when new work is inside a collapsed block', async () => {
		const landed = recordLanding();
		messages.set(rows(work('w1', 1, '10:00:00'), work('w2', 2, '10:01:00'), said('s3', 3, '10:02:00')));
		visitDividers.set({ [THREAD]: divider(2) });
		threadUpdateCards.set({ [THREAD]: { fromSeq: 0, cards: [] } });
		render();
		await tick(); flushSync();
		runFrames();
		expect(landed).toHaveLength(1);
		expect(landed[0].hasAttribute('data-visit-divider')).toBe(true);
	});

	it('lands the divider line when it sits between rows', async () => {
		const landed = recordLanding();
		messages.set(rows(work('w1', 1, '10:00:00'), said('s2', 2, '10:01:00'), said('s3', 3, '10:02:00')));
		visitDividers.set({ [THREAD]: divider(3) });
		render();
		await tick(); flushSync();
		runFrames();
		expect(landed).toHaveLength(1);
		expect(landed[0].hasAttribute('data-visit-divider')).toBe(true);
	});

	it('opens at the bottom when nothing is new', async () => {
		const landed = recordLanding();
		messages.set(rows(work('w1', 1, '10:00:00'), said('s2', 2, '10:01:00')));
		visitDividers.set({ [THREAD]: { divider: null, failed: false } });
		render();
		await tick(); flushSync();
		runFrames();
		expect(landed).toEqual([]);
	});

	it('yields to whatever the user does first', async () => {
		const landed = recordLanding();
		messages.set(rows(work('w1', 1, '10:00:00'), work('w2', 2, '10:01:00'), said('s3', 3, '10:02:00')));
		render();
		scroller().dispatchEvent(new Event('wheel'));
		visitDividers.set({ [THREAD]: divider(2) });
		flushSync();
		await tick(); flushSync();
		runFrames();
		expect(landed).toEqual([]);
	});

	it('does not jump again while the thread stays open', async () => {
		const landed = recordLanding();
		messages.set(rows(work('w1', 1, '10:00:00'), work('w2', 2, '10:01:00'), said('s3', 3, '10:02:00')));
		visitDividers.set({ [THREAD]: divider(2) });
		render();
		await tick(); flushSync();
		runFrames(); runFrames();
		expect(landed).toHaveLength(1);
		messages.update((all) => [...all, ...rows(said('s4', 4, '10:03:00'))]);
		flushSync(); await tick(); flushSync();
		runFrames();
		expect(landed).toHaveLength(1);
	});

	it('keeps landing targets and the pinned header under the app header, below the phone safe area', () => {
		// jsdom has no layout: assert the one shared offset every pinned or landing element reads.
		expect(messageListSource).toMatch(/--sticky-top: calc\(var\(--header-bottom\) \+ 8px\)/);
		expect(workBlockSource).toMatch(/top: var\(--sticky-top, 0px\)/);
		expect(workBlockSource).toMatch(/scroll-margin-top: var\(--sticky-top, 0px\)/);
		expect(messageListSource).toMatch(/position: sticky; top: var\(--sticky-top, 0px\); z-index: 5;/);
		expect(workBlockSource).toMatch(/z-index: 4;/);
	});

	it('keeps the lead-in under the header out of the scroller padding, which a sticky offset would add to', () => {
		const messagesRules = messageListSource.slice(messageListSource.indexOf('.messages {'));
		expect(messagesRules).toMatch(/\.messages::before \{ content: ''; display: block; flex: none; height: var\(--messages-lead\); \}/);
		expect(messagesRules).not.toMatch(/padding-top:|padding: calc\(var\(--header-height\)/);
	});
});

describe('G2 pending area in Focus', () => {
	const waitingWake = { ...auto('waiting1', null, 'p', null), thread_id: THREAD, delivery_state: 'queued', write_outcome: null };
	const waitingDigest = { ...compact('waiting-digest', 0, 'p'), seq: null, thread_id: THREAD, delivery_state: 'queued' };

	it('shows a context update that is only waiting nowhere', () => {
		messages.set(rows(work('w1', 1, '10:00:00'), waitingWake, waitingDigest));
		render();
		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(host.querySelector('[data-message-id="waiting1"]')).toBeNull();
		expect(host.querySelector('[data-wake-retry]')).toBeNull();
	});

	it('keeps one line with its Retry for a wake that failed', () => {
		const failed = { ...auto('failed1', 2, 'p', null), thread_id: THREAD, delivery_state: 'failed', delivery_error: 'Refused', write_outcome: 'proved_no_write' };
		messages.set(rows(work('w1', 1, '10:00:00'), failed));
		render();
		const lines = host.querySelectorAll('.pending-input-region [data-wake-retry]');
		expect(lines).toHaveLength(1);
		expect(lines[0].textContent).toContain("Couldn't deliver: Resumed after plan limit pause");
		expect(lines[0].querySelector('[data-delivery-action="retry"]')?.textContent).toBe('Retry');
		// The block does not repeat it.
		expect(host.querySelector('.transcript-fold [data-message-id="failed1"]')).toBeNull();
	});

	it('keeps a wake parked on a CORSIA defect retryable, and offers Retry anyway when uncertain', () => {
		const uncertain = { ...auto('uncertain1', 2, 'p', null), thread_id: THREAD, delivery_state: 'ambiguous', write_outcome: 'ambiguous' };
		messages.set(rows(work('w1', 1, '10:00:00'), uncertain));
		render();
		expect(host.querySelector('[data-wake-retry] [data-delivery-action="retry-anyway"]')?.textContent).toBe('Retry anyway');
	});

	it('leaves your own waiting message as it is', () => {
		const mine = { id: 'mine', thread_id: THREAD, role: 'user', content: 'steer', delivery_state: 'queued', delivery_error: null, delivery_revision: 1, input_order: 1, metadata: { input_id: 'i1' }, _optimistic: true, created_at: '2026-10-02T10:05:00Z' };
		messages.set(rows(work('w1', 1, '10:00:00'), mine));
		render();
		expect(host.querySelector('.pending-input-region [data-message-id="mine"]')).not.toBeNull();
	});
});
