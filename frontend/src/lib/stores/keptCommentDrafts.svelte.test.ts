import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { get } from 'svelte/store';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { documentDraftKey, keptCommentDrafts, replyDraftKey, showsDraft } from './keptCommentDrafts.svelte';
import { toasts } from './toast';

afterEach(() => keptCommentDrafts.reset());

const scope = documentDraftKey({ projectId: 'project-1', path: 'notes.md' });

describe('a reply kept while it is typed', () => {
	it('keeps its text, tags and caret until its field is emptied', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'half a reply', tags);
		keptCommentDrafts.select(key, [2, 6]);
		const kept = keptCommentDrafts.get(key)!;
		expect(kept.body).toBe('half a reply');
		expect(kept.mentionSelection).toBe(tags);
		expect(kept.selection).toEqual([2, 6]);
		// A reply has no Cancel: emptying its field clears it, its id and reason included.
		keptCommentDrafts.claim(key, 'half a reply', tags);
		keptCommentDrafts.fail(key, 'half a reply', tags, 'Couldn’t post');
		keptCommentDrafts.keepReply(key, '', tags);
		expect(keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('keeps each thread’s reply apart, and a document’s apart from another’s', () => {
		const other = documentDraftKey({ projectId: 'project-1', path: 'other.md' });
		keptCommentDrafts.keepReply(replyDraftKey(scope, 'comment-1'), 'one', createMentionSelection());
		keptCommentDrafts.keepReply(replyDraftKey(scope, 'comment-2'), 'two', createMentionSelection());
		expect(keptCommentDrafts.get(replyDraftKey(scope, 'comment-1'))?.body).toBe('one');
		expect(keptCommentDrafts.get(replyDraftKey(scope, 'comment-2'))?.body).toBe('two');
		expect(keptCommentDrafts.get(replyDraftKey(other, 'comment-1'))).toBeUndefined();
	});

	it('keeps the reason a post failed, with the text, caret and the one id every press posts under', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'half a reply', tags);
		keptCommentDrafts.select(key, [4, 4]);
		const first = keptCommentDrafts.claim(key, 'half a reply', tags);
		expect(first.again).toBe(false);
		keptCommentDrafts.fail(key, 'half a reply', tags, 'Couldn’t post the comment. Your draft is kept. Try again.');
		expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'half a reply', error: 'Couldn’t post the comment. Your draft is kept. Try again.', clientId: first.clientId, selection: [4, 4] });
		keptCommentDrafts.clearError(key);
		expect(keptCommentDrafts.claim(key, 'half a reply', tags)).toEqual({ clientId: first.clientId, again: true });
		// Its earlier version was stored under that id: the changes post as a new reply, under a fresh id.
		keptCommentDrafts.fail(key, 'half a reply, changed', tags, 'Your earlier version was posted. Your changes are kept here.', true);
		const fresh = keptCommentDrafts.claim(key, 'half a reply, changed', tags);
		expect(fresh.again).toBe(false);
		expect(fresh.clientId).not.toBe(first.clientId);
	});

	it('goes once the server stores it, but text typed while it posted stays, under a fresh id', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'first ', tags);
		const press = keptCommentDrafts.claim(key, 'first ', tags);
		expect(keptCommentDrafts.posted(key, 'first', [])).toBe(true);
		expect(keptCommentDrafts.get(key)).toBeUndefined();

		keptCommentDrafts.keepReply(key, 'first', tags);
		const again = keptCommentDrafts.claim(key, 'first', tags);
		expect(again.clientId).not.toBe(press.clientId);
		keptCommentDrafts.keepReply(key, 'first and more', tags);
		expect(keptCommentDrafts.posted(key, 'first', [])).toBe(false);
		expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'first and more' });
		expect(keptCommentDrafts.get(key)?.clientId).toBeUndefined();
		expect(keptCommentDrafts.claim(key, 'first and more', tags).clientId).not.toBe(again.clientId);
	});

	it('a post stored while its tags changed keeps the new tags, in a copy the field that pressed cannot clear', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'Ask @Ada', tags);
		keptCommentDrafts.claim(key, 'Ask @Ada', tags);
		tags.update('Ask @Ada'); tags.select('agent-ada', 4, '@Ada');
		keptCommentDrafts.keepReply(key, 'Ask @Ada', tags);
		expect(keptCommentDrafts.posted(key, 'Ask @Ada', [])).toBe(false);
		// The field that pressed empties its own tags once its post is stored.
		tags.clear();
		const kept = keptCommentDrafts.get(key)!;
		expect(kept.body).toBe('Ask @Ada');
		expect(kept.mentionSelection.occurrences(kept.body).map((item) => item.agent_id)).toEqual(['agent-ada']);
		expect(kept.clientId).toBeUndefined();
	});

	it('typing in one reply re-runs nothing that reads another thread’s reply', () => {
		const typed = replyDraftKey(scope, 'comment-1');
		const kept = replyDraftKey(scope, 'comment-2');
		const empty = replyDraftKey(scope, 'comment-3');
		keptCommentDrafts.keepReply(kept, 'kept', createMentionSelection());
		const runs = { typed: 0, kept: 0, empty: 0 };
		const stop = $effect.root(() => {
			$effect(() => { keptCommentDrafts.get(typed); runs.typed++; });
			$effect(() => { keptCommentDrafts.get(kept); runs.kept++; });
			$effect(() => { keptCommentDrafts.get(empty); runs.empty++; });
		});
		flushSync();
		expect(runs).toEqual({ typed: 1, kept: 1, empty: 1 });
		const tags = createMentionSelection();
		// The first letter, every letter after it, the caret, and emptying the field again.
		for (const text of ['h', 'ha', 'hal', 'half']) { keptCommentDrafts.keepReply(typed, text, tags); flushSync(); }
		keptCommentDrafts.select(typed, [4, 4]); flushSync();
		keptCommentDrafts.keepReply(typed, '', tags); flushSync();
		expect(runs.typed).toBe(7);
		expect(runs.kept).toBe(1);
		expect(runs.empty).toBe(1);
		stop();
	});
});

describe('a box that shows a draft, read once when a post fails', () => {
	/** A box laid out at `rect` (jsdom lays nothing out, so each box reports its own). */
	const box = (rect: DOMRect) => {
		const node = document.createElement('div');
		node.getBoundingClientRect = () => rect;
		return node;
	};
	const inView = () => new DOMRect(20, 100, 300, 80);
	const key = replyDraftKey(scope, 'comment-1');

	it('is on screen only while a box for that draft is mounted and takes room inside the window', () => {
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
		const drop = keptCommentDrafts.shown(key, box(inView()));
		expect(keptCommentDrafts.onScreen(key)).toBe(true);
		// Another draft's box says nothing about this one.
		expect(keptCommentDrafts.onScreen(replyDraftKey(scope, 'comment-2'))).toBe(false);
		drop();
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
	});

	it.each([
		['a hidden or collapsed box (no size)', new DOMRect(20, 100, 0, 0)],
		['a box with height but no width', new DOMRect(20, 100, 0, 80)],
		['a box scrolled above the window', new DOMRect(20, -200, 300, 80)],
		['a box scrolled below the window', new DOMRect(20, window.innerHeight + 10, 300, 80)],
		['a box off to the side', new DOMRect(window.innerWidth + 10, 100, 300, 80)],
	])('is not on screen for %s', (_name, rect) => {
		keptCommentDrafts.shown(key, box(rect));
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
	});

	it('counts any one of several boxes for the same draft', () => {
		keptCommentDrafts.shown(key, box(new DOMRect(20, -200, 300, 80)));
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
		keptCommentDrafts.shown(key, box(inView()));
		expect(keptCommentDrafts.onScreen(key)).toBe(true);
	});

	it('reads the visual viewport, so a box under a phone keyboard is not on screen', () => {
		const original = Object.getOwnPropertyDescriptor(window, 'visualViewport');
		// The keyboard leaves the top 300px; the box sits at 400, inside the layout viewport but under the keyboard.
		Object.defineProperty(window, 'visualViewport', { configurable: true, value: { offsetLeft: 0, offsetTop: 0, width: 393, height: 300 } });
		try {
			keptCommentDrafts.shown(key, box(new DOMRect(10, 400, 300, 80)));
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
			keptCommentDrafts.shown(key, box(new DOMRect(10, 200, 300, 80)));
			expect(keptCommentDrafts.onScreen(key)).toBe(true);
		} finally {
			if (original) Object.defineProperty(window, 'visualViewport', original);
			else delete (window as { visualViewport?: unknown }).visualViewport;
		}
	});

	it('reads the box\'s own scroller, so a box it has scrolled out of its view is not on screen', () => {
		// A sheet or column that scrolls, showing 0-300 of the window; the box sits inside the window but below what it shows.
		const sheet = box(new DOMRect(0, 0, 393, 300));
		sheet.style.overflowY = 'auto';
		const inner = document.createElement('div');
		sheet.append(inner);
		document.body.append(sheet);
		try {
			const hidden = box(new DOMRect(10, 400, 300, 80));
			inner.append(hidden);
			const drop = keptCommentDrafts.shown(key, hidden);
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
			drop();
			// Partly in the part it shows still counts: the person sees the box.
			const showing = box(new DOMRect(10, 260, 300, 80));
			inner.append(showing);
			keptCommentDrafts.shown(key, showing);
			expect(keptCommentDrafts.onScreen(key)).toBe(true);
		} finally {
			sheet.remove();
		}
	});

	/** A box in view inside `host` on the page, so it inherits and is clipped as it would be drawn. */
	const inside = (host: HTMLElement) => {
		document.body.append(host);
		const node = box(inView());
		host.append(node);
		return node;
	};

	it('is not on screen while hidden, on the box or inherited from a surface over it', () => {
		const host = document.createElement('div');
		const node = inside(host);
		try {
			keptCommentDrafts.shown(key, node);
			expect(keptCommentDrafts.onScreen(key)).toBe(true);
			host.style.visibility = 'hidden';
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
			host.style.visibility = '';
			node.style.visibility = 'hidden';
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
		} finally { host.remove(); }
	});

	it('is not on screen inside an inert surface', () => {
		const host = document.createElement('div');
		const node = inside(host);
		try {
			keptCommentDrafts.shown(key, node);
			host.setAttribute('inert', '');
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
			host.removeAttribute('inert');
			expect(keptCommentDrafts.onScreen(key)).toBe(true);
		} finally { host.remove(); }
	});

	it('is not on screen inside a Popover clipped away whole before it is placed', () => {
		const host = document.createElement('div');
		const node = inside(host);
		try {
			keptCommentDrafts.shown(key, node);
			host.style.clipPath = 'inset(100%)';
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
			host.style.clipPath = '';
			expect(keptCommentDrafts.onScreen(key)).toBe(true);
		} finally { host.remove(); }
	});

	it('is registered by `use:showsDraft` while mounted, moves with its key, and is gone on unmount', () => {
		const node = box(inView());
		const action = showsDraft(node, key);
		expect(keptCommentDrafts.onScreen(key)).toBe(true);
		const other = replyDraftKey(scope, 'comment-2');
		action.update(other);
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
		expect(keptCommentDrafts.onScreen(other)).toBe(true);
		action.destroy();
		expect(keptCommentDrafts.onScreen(other)).toBe(false);
	});

	it('registers nothing for a box with no key', () => {
		showsDraft(box(inView()), '');
		expect(keptCommentDrafts.onScreen('')).toBe(false);
	});
});

describe('the toast that told a failure while the draft’s box was not on screen', () => {
	const box = (rect: () => DOMRect) => {
		const node = document.createElement('div');
		node.getBoundingClientRect = rect;
		return node;
	};
	const inView = () => new DOMRect(20, 100, 300, 80);
	const below = () => new DOMRect(20, window.innerHeight + 10, 300, 80);
	const key = replyDraftKey(scope, 'comment-1');
	const REASON = 'Couldn’t post the comment. Your draft is kept. Try again.';
	const up = (id: string) => get(toasts).some((toast) => toast.id === id);
	/** A draft whose post failed with no box on screen, so a toast told it. */
	function failedAway(): string {
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'half a reply', tags);
		keptCommentDrafts.claim(key, 'half a reply', tags);
		keptCommentDrafts.fail(key, 'half a reply', tags, REASON);
		const id = toasts.show(REASON, 'error');
		keptCommentDrafts.toast(key, id);
		return id;
	}
	afterEach(() => { for (const toast of get(toasts)) toasts.dismiss(toast.id); vi.unstubAllGlobals(); });

	it('stays while the draft’s box mounts hidden (inherited visibility, an inert surface, an unplaced Popover)', () => {
		const id = failedAway();
		const hosts = [['visibility', 'hidden'], ['inert', ''], ['clip-path', 'inset(100%)']].map(([name, value]) => {
			const host = document.body.appendChild(document.createElement('div'));
			if (name === 'inert') host.setAttribute('inert', value);
			else host.style.setProperty(name, value);
			host.append(box(inView));
			return host;
		});
		try {
			for (const host of hosts) showsDraft(host.firstElementChild!, key);
			expect(up(id)).toBe(true);
			expect(keptCommentDrafts.get(key)?.toast).toBe(id);
		} finally { for (const host of hosts) host.remove(); }
	});

	it('goes once the draft’s own box comes on screen, which says why instead; the reason stays with the draft', () => {
		const id = failedAway();
		expect(up(id)).toBe(true);
		// Another draft's box coming on screen leaves it.
		keptCommentDrafts.shown(replyDraftKey(scope, 'comment-2'), box(inView));
		expect(up(id)).toBe(true);
		showsDraft(box(inView), key);
		expect(up(id)).toBe(false);
		expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'half a reply', error: REASON });
		expect(keptCommentDrafts.get(key)?.toast).toBeUndefined();
	});

	it('stays while the box mounts off screen, and goes if the box is on screen a frame later (one that slides in)', () => {
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (run: FrameRequestCallback) => frames.push(run));
		const id = failedAway();
		let rect = below;
		keptCommentDrafts.shown(key, box(() => rect()));
		expect(up(id)).toBe(true);
		rect = inView;
		for (const run of frames.splice(0)) run(0);
		expect(up(id)).toBe(false);
	});

	it('stays for a box still off screen a frame later, or unmounted by then', () => {
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (run: FrameRequestCallback) => frames.push(run));
		const id = failedAway();
		keptCommentDrafts.shown(key, box(below));
		let rect = below;
		const drop = keptCommentDrafts.shown(key, box(() => rect()));
		drop();
		rect = inView;
		for (const run of frames.splice(0)) run(0);
		expect(up(id)).toBe(true);
		expect(keptCommentDrafts.get(key)?.toast).toBe(id);
	});

	it('stays for carried text with no failure of its own, whose box does not say why', () => {
		keptCommentDrafts.keepReply(key, 'carried over', createMentionSelection());
		const id = toasts.show('The diagram changed. Draw the box again.', 'error');
		keptCommentDrafts.toast(key, id);
		keptCommentDrafts.shown(key, box(inView));
		expect(up(id)).toBe(true);
	});

	it('is replaced by a later failure’s toast, goes when a later failure is told in the box, and goes when the draft does', () => {
		const first = failedAway();
		// A new press keeps it up until that press ends.
		keptCommentDrafts.clearError(key);
		expect(keptCommentDrafts.get(key)?.toast).toBe(first);
		keptCommentDrafts.fail(key, 'half a reply', createMentionSelection(), REASON);
		// The store shows one ordinary toast at a time, so whether the draft let go of the first is read from its dismissals.
		const dismiss = vi.spyOn(toasts, 'dismiss');
		try {
			const second = 'a toast the store no longer shows';
			keptCommentDrafts.toast(key, second);
			expect(dismiss).toHaveBeenCalledExactlyOnceWith(first);
			expect(keptCommentDrafts.get(key)?.toast).toBe(second);
			keptCommentDrafts.toast(key, null);
			expect(dismiss).toHaveBeenLastCalledWith(second);
			expect(keptCommentDrafts.get(key)?.toast).toBeUndefined();
		} finally {
			dismiss.mockRestore();
		}

		const posted = failedAway();
		keptCommentDrafts.posted(key, 'half a reply', []);
		expect(up(posted)).toBe(false);
		const edited = failedAway();
		keptCommentDrafts.keepReply(key, 'half a reply, and more', createMentionSelection());
		expect(keptCommentDrafts.posted(key, 'half a reply', [])).toBe(false);
		expect(up(edited)).toBe(false);
		const emptied = failedAway();
		keptCommentDrafts.keepReply(key, '', createMentionSelection());
		expect(up(emptied)).toBe(false);
		const cancelled = failedAway();
		keptCommentDrafts.forget(key);
		expect(up(cancelled)).toBe(false);
	});
});
