import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread, { type CommentThreadData } from './CommentThread.svelte';

type Reply = NonNullable<CommentThreadData['replies']>[number];
const reply = (id: string, createdAt = '2026-10-16T18:00:00Z'): Reply => ({ id, author: 'agent-1', authorKind: 'agent', text: `reply ${id}`, at: 'later', createdAt });
const state = $state<{ comment: CommentThreadData }>({ comment: { id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] } });

class FakeObserver {
	static all: FakeObserver[] = [];
	// Geometry of whatever is observed at the moment observe() runs; a callback reports it.
	static visibleOnObserve = false;
	disconnected = false;
	target: Element | null = null;
	constructor(private callback: IntersectionObserverCallback, options?: IntersectionObserverInit) { this.root = options?.root ?? null; FakeObserver.all.push(this); }
	observe(target: Element) {
		this.target = target;
		if (FakeObserver.visibleOnObserve) this.report(true);
	}
	report(visible: boolean) {
		if (this.disconnected || !this.target) return;
		this.callback([{ isIntersecting: visible, intersectionRatio: visible ? 1 : 0, target: this.target } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
	}
	reportTall(shown = 599, viewport = 600) {
		if (!this.target) return;
		this.callback([{ isIntersecting: true, intersectionRatio: 0.5, target: this.target, intersectionRect: { height: shown }, boundingClientRect: { height: shown / 0.5 }, rootBounds: { top: 0, height: viewport } } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
	}
	disconnect() { this.disconnected = true; }
	unobserve() {}
	takeRecords() { return []; }
	root: Element | Document | null = null; rootMargin = ''; thresholds = [];
	static live() { return FakeObserver.all.filter((observer) => !observer.disconnected); }
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const surface = $state({ active: true });
function setVisibility(value: 'visible' | 'hidden') {
	Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => value });
}
function render(onViewed: (id: string, replyId: string) => Promise<void>) {
	vi.stubGlobal('IntersectionObserver', FakeObserver);
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(CommentThread, { target: host, props: { get comment() { return state.comment; }, get onViewed() { return surface.active ? onViewed : undefined; } } });
	flushSync();
}
function arrive(comment: Partial<CommentThreadData>) {
	state.comment = { ...state.comment, ...comment }; flushSync();
}
const newMarks = () => host.querySelectorAll('.ct-new').length;
async function settle() { await Promise.resolve(); flushSync(); await Promise.resolve(); flushSync(); }
const dwell = () => new Promise((resolve) => setTimeout(resolve, 1_550));

afterEach(() => {
	if (component) unmount(component);
	component = null; host?.remove();
	FakeObserver.all = []; FakeObserver.visibleOnObserve = false; surface.active = true;
	state.comment = { id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] };
	delete (document as unknown as Record<string, unknown>).visibilityState;
	vi.unstubAllGlobals();
});

describe('CommentThread stamping', () => {
	it('stamps a tall reply filling the visible area', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		FakeObserver.live()[0].reportTall();
		await dwell();
		expect(onViewed).toHaveBeenCalledWith('c1', 'r1');
	});

	it('stamps a tall reply filling a scroller shorter than the viewport, and not one that falls short of it', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		const style = vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => ({ overflowY: element === host ? 'auto' : 'visible' }) as CSSStyleDeclaration);
		render(onViewed);
		vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 400, height: 300 } as DOMRect);
		FakeObserver.live()[0].reportTall(200, 800);
		await dwell();
		expect(onViewed).not.toHaveBeenCalled();
		FakeObserver.live()[0].reportTall(300, 800);
		await dwell();
		expect(onViewed).toHaveBeenCalledWith('c1', 'r1');
		style.mockRestore();
	});

	it('preserves an in-progress dwell across an identical unread projection', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		FakeObserver.live()[0].report(true);
		await new Promise((resolve) => setTimeout(resolve, 500));
		arrive({ newReplyIds: ['r1'] });
		await new Promise((resolve) => setTimeout(resolve, 1100));
		expect(onViewed).toHaveBeenCalledWith('c1', 'r1');
	});
	it('preserves an existing dwell when another unread reply arrives', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		FakeObserver.live()[0].report(true);
		await new Promise((resolve) => setTimeout(resolve, 500));
		arrive({ replies: [reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] });
		await new Promise((resolve) => setTimeout(resolve, 1100));
		expect(onViewed).toHaveBeenCalledWith('c1', 'r1');
	});

	it('observes against the viewport even inside a scroll container', () => {
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		const style = vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => ({ overflowY: element === host ? 'auto' : 'visible' }) as CSSStyleDeclaration);
		render(vi.fn().mockResolvedValue(undefined));
		expect(FakeObserver.live()[0].root).toBeNull();
		style.mockRestore();
	});
	it('observes each unread reply so a later one cannot clear an unseen earlier reply', () => {
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] };
		render(vi.fn().mockResolvedValue(undefined));
		expect(FakeObserver.live().map((observer) => (observer.target as HTMLElement).dataset.replyId)).toEqual(['r1', 'r2']);
	});

	it('does not stamp when only the newest unread reply is visible', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] };
		render(onViewed);
		FakeObserver.live()[1].report(true);
		await dwell();
		expect(onViewed).not.toHaveBeenCalled();
	});

	it('advances through a later reply already seen once the earlier reply dwells', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] };
		render(onViewed);
		FakeObserver.live()[1].report(true);
		await dwell();
		FakeObserver.live()[1].report(false);
		FakeObserver.live()[0].report(true);
		await dwell();
		expect(onViewed.mock.calls).toEqual([['c1', 'r2']]);
	});

	it('stamps only the earliest unread reply when it alone is visible', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] };
		render(onViewed);
		FakeObserver.live()[0].report(true);
		await dwell();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1']]);
	});

	it('stamps a resolved thread\'s unread reply only once the thread is expanded and the reply dwells', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, resolved: true, replies: [reply('r1')], newReplyIds: ['r1'] };
		FakeObserver.visibleOnObserve = true;
		render(onViewed);
		expect(FakeObserver.live()).toHaveLength(0);
		host.querySelector<HTMLButtonElement>('.ct-res-head')!.click(); flushSync();
		expect(newMarks()).toBe(1);
		await dwell();
		expect(onViewed).toHaveBeenCalledWith('c1', 'r1');
	});

	it('cancels a resolved thread\'s dwell when it is collapsed before the reply has dwelled', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, resolved: true, replies: [reply('r1')], newReplyIds: ['r1'] };
		FakeObserver.visibleOnObserve = true;
		render(onViewed);
		const head = host.querySelector<HTMLButtonElement>('.ct-res-head')!;
		head.click(); flushSync();
		await new Promise((resolve) => setTimeout(resolve, 500));
		head.click(); flushSync();
		await settle();
		expect(FakeObserver.live()).toHaveLength(0);
		await dwell();
		expect(onViewed).not.toHaveBeenCalled();
	});

	it('cancels dwell while the comment surface is covered and restarts on return', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		FakeObserver.visibleOnObserve = true;
		render(onViewed);
		await new Promise((resolve) => setTimeout(resolve, 500));
		surface.active = false; flushSync();
		await dwell();
		expect(onViewed).not.toHaveBeenCalled();
		surface.active = true; flushSync();
		await dwell();
		expect(onViewed).toHaveBeenCalledWith('c1', 'r1');
	});

	it('does not stamp a reply that arrives while the previous newest was visible, until its own element is fully visible', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		FakeObserver.live()[0].report(true);
		await dwell();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1']]);

		// The store answers the stamp, then another reply arrives; its element has not reported yet.
		arrive({ newReplyIds: ['r2'], replies: [reply('r1'), reply('r2', '2026-10-16T18:01:00Z')] });
		await settle();
		expect(onViewed).toHaveBeenCalledTimes(1);
		expect(FakeObserver.live()).toHaveLength(1);
		FakeObserver.live()[0].report(false);
		expect(onViewed).toHaveBeenCalledTimes(1);
		FakeObserver.live()[0].report(true);
		await dwell();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1'], ['c1', 'r2']]);
	});

	it('does not stamp in a hidden document, and stamps once when it becomes visible with the reply still fully visible', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		setVisibility('hidden');
		FakeObserver.visibleOnObserve = true;
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		expect(onViewed).not.toHaveBeenCalled();

		setVisibility('visible');
		document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); flushSync();
		await dwell();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1']]);
		document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); flushSync();
		// A reply stamped from this mount is not stamped again by a later visibility change.
		expect(onViewed).toHaveBeenCalledTimes(1);
	});

	it('does not stamp from the old geometry when the document returns with the reply scrolled out of view', () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		setVisibility('hidden');
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		FakeObserver.live()[0].report(true);
		setVisibility('visible');
		FakeObserver.visibleOnObserve = false;
		document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); flushSync();
		expect(onViewed).not.toHaveBeenCalled();
	});

	it('retries a failed stamp on the next visibility change and not in a loop', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const onViewed = vi.fn().mockRejectedValue(new Error('offline'));
		FakeObserver.visibleOnObserve = true;
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		await dwell();
		await vi.waitFor(() => expect(warn).toHaveBeenCalled());
		await settle(); await settle();
		expect(onViewed).toHaveBeenCalledTimes(1);
		document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); flushSync();
		await dwell();
		await vi.waitFor(() => expect(onViewed).toHaveBeenCalledTimes(2));
		await settle();
		expect(onViewed).toHaveBeenCalledTimes(2);
		warn.mockRestore();
	});

	it('stamps both visible unread replies together and keeps the collapse point stable', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		const replies = Array.from({ length: 6 }, (_, index) => reply(`r${index}`, `2026-10-16T18:0${index}:00Z`));
		state.comment = { ...state.comment, replies, newReplyIds: ['r4', 'r5'] };
		render(onViewed);
		expect(newMarks()).toBe(2);
		expect(host.textContent).toContain('Show 4 earlier replies');

		for (const observer of FakeObserver.live()) observer.report(true);
		await dwell();
		expect(onViewed.mock.calls).toEqual([['c1', 'r5']]);
		arrive({ newReplyIds: [] });
		expect(newMarks()).toBe(0);
		expect(host.textContent).toContain('Show 4 earlier replies');
		expect(host.textContent).toContain('reply r4');
		expect(host.textContent).not.toContain('reply r0');
	});

	it('shows only the server-reported unread reply while the thread is open', () => {
		const replies = [reply('r1'), reply('r2', '2026-10-16T18:01:00Z')];
		state.comment = { ...state.comment, replies, newReplyIds: ['r1'] };
		render(vi.fn().mockResolvedValue(undefined));
		expect(newMarks()).toBe(1);
		arrive({ newReplyIds: ['r2'] });
		expect(newMarks()).toBe(1);
		arrive({ newReplyIds: [] });
		expect(newMarks()).toBe(0);
	});
});
