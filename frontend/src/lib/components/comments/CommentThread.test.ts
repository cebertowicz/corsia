import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread from './CommentThread.svelte';
import type { CommentThreadData } from './CommentThreadData';
import CommentComposer from './CommentComposer.svelte';
import { agentColor } from '$lib/docPresence';

const hexToRgb = (hex: string) => `rgb(${[1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(', ')})`;
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
function thread(overrides: Partial<CommentThreadData> = {}): CommentThreadData {
	return { id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [], ...overrides };
}
function render(comment: CommentThreadData, readOnly = false, extra: Record<string, unknown> = {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	const onReply = vi.fn().mockResolvedValue(undefined); const onResolve = vi.fn(); const onAgree = vi.fn();
	component = mount(CommentThread, { target: host, props: { comment, readOnly, onReply, onResolve, onAgree, ...extra } });
	flushSync(); return { onReply, onResolve, onAgree };
}
const reply = { author: 'agent-1', authorKind: 'agent' as const, text: 'Could we keep the old wording?', at: 'later' };
function type(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
	input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
}
const button = (selector: string) => host.querySelector<HTMLButtonElement>(selector)!;
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });

describe('CommentThread', () => {
	it.each([
		['expanded resolved comment', { resolved: true }],
		['open suggestion', { replacement: 'new text' }],
		['decided suggestion with an open conversation', { replacement: 'new text', decision: 'accepted' as const }],
	])('exposes the discussion for sizing on an %s', (_label, state) => {
		render(thread({ ...state, replies: [reply] }));
		if ('resolved' in state && state.resolved) { button('.ct-res-head').click(); flushSync(); }
		expect(host.querySelector('[data-discussion-original]')?.textContent).toContain('note');
		expect(host.querySelector('[data-discussion-replies]')?.textContent).toContain(reply.text);
		expect(host.querySelector('.ct-input')).not.toBeNull();
	});

	it('posts a reply and clears the input', async () => {
		const { onReply } = render(thread());
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, '  looks right  ');
		button('.ct-send').click();
		await vi.waitFor(() => expect(input.value).toBe(''));
		expect(onReply).toHaveBeenCalledWith('c1', 'looks right', [], []);
	});

	describe('the line under the reply field', () => {
		it('names the agents the reply wakes, and says so when there are none', () => {
			render(thread({ replyGoesTo: [{ id: 'a1', name: 'Docs lane' }, { id: 'a2', name: 'Release lane' }] }));
			expect(host.querySelector('.rl')?.textContent).toContain('Goes to');
			expect(Array.from(host.querySelectorAll('.rl-name'), (name) => name.textContent)).toEqual(['Docs lane', 'Release lane']);
			unmount(component!); host.remove();
			render(thread({ replyGoesTo: [] }));
			expect(host.querySelector('.rl')?.textContent).toContain('No agent gets this reply.');
		});
		it('is absent where the caller does not know who gets a reply', () => {
			render(thread());
			expect(host.querySelector('.rl')).toBeNull();
		});
	});

	it('keeps focus in the reply field when a clicked Send goes away after posting', async () => {
		render(thread());
		const input = host.querySelector<HTMLTextAreaElement>('.ct-input')!;
		type(input, 'looks right');
		button('.ct-send').focus();
		button('.ct-send').click();
		await vi.waitFor(() => expect(host.querySelector('.ct-send')).toBeNull());
		expect(document.activeElement).toBe(input);
	});

	it('draws no Reply field while hideReply is set, unless a reply is half typed', () => {
		render(thread({ replies: [reply] }), false, { hideReply: true });
		expect(host.querySelector('.ct-input')).toBeNull();
		expect(host.textContent).toContain('1 reply');
		unmount(component!); host.remove();
		render(thread(), false, { hideReply: true, replyText: 'half a reply' });
		expect(host.querySelector<HTMLTextAreaElement>('.ct-input')?.value).toBe('half a reply');
	});

	it('keeps a draft typed while the previous reply was still sending', async () => {
		let finish!: () => void;
		const onReply = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
		render(thread(), false, { onReply, chatSurface: true });
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, 'first');
		button('.ct-send').click();
		await vi.waitFor(() => expect(onReply).toHaveBeenCalledWith('c1', 'first', [], []));
		type(input, 'second thought');
		finish();
		await vi.waitFor(() => expect(button('.ct-send').getAttribute('aria-busy')).toBe('false'));
		expect(input.value).toBe('second thought');
	});

	it('restores a reply selection across remounts and saves a click-placed caret', async () => {
		const onReplySelection = vi.fn();
		render(thread(), false, { showReply: true, replyText: 'draft text', replySelection: [2, 5], onReplySelection });
		await vi.waitFor(() => expect(host.querySelector<HTMLTextAreaElement>('.ct-input')).not.toBeNull());
		let input = host.querySelector<HTMLTextAreaElement>('.ct-input')!;
		expect([input.selectionStart, input.selectionEnd]).toEqual([2, 5]);
		input.setSelectionRange(7, 7);
		input.dispatchEvent(new Event('pointerup', { bubbles: true }));
		expect(onReplySelection).toHaveBeenLastCalledWith([7, 7]);
		unmount(component!); component = null; host.remove();
		render(thread(), false, { showReply: true, replyText: 'draft text', replySelection: [7, 7], onReplySelection });
		await vi.waitFor(() => expect(host.querySelector<HTMLTextAreaElement>('.ct-input')).not.toBeNull());
		input = host.querySelector<HTMLTextAreaElement>('.ct-input')!;
		expect([input.selectionStart, input.selectionEnd]).toEqual([7, 7]);
	});

	it('clears the owned selection when a reply is sent', async () => {
		const onReplySelection = vi.fn();
		const { onReply } = render(thread(), false, { onReplySelection });
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, 'ship it');
		input.setSelectionRange(1, 4);
		input.dispatchEvent(new Event('select', { bubbles: true }));
		button('.ct-send').click();
		await vi.waitFor(() => expect(onReply).toHaveBeenCalledWith('c1', 'ship it', [], []));
		await vi.waitFor(() => expect(onReplySelection).toHaveBeenLastCalledWith(null));
	});

	it('clears the owned selection when the draft is emptied by hand', () => {
		const onReplySelection = vi.fn();
		render(thread(), false, { onReplySelection });
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, 'draft');
		expect(onReplySelection).toHaveBeenLastCalledWith([5, 5]);
		type(input, '');
		expect(onReplySelection).toHaveBeenLastCalledWith(null);
	});

	it('keeps the reply field visible on every open thread', () => {
		render(thread());
		expect(host.querySelector('.ct-compose.open .ct-input')).not.toBeNull();
		expect(host.querySelector('.ct-reply-open')).toBeNull();
	});

	it('keeps a sheet reply visible after it expands', async () => {
		const original = HTMLElement.prototype.scrollIntoView;
		const scrollIntoView = vi.fn();
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView });
		try {
			render(thread(), false, { showReply: true, keepReplyVisible: true });
			await vi.waitFor(() => expect(host.querySelector('.ct-input')).not.toBeNull());
			await vi.waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' }));
		} finally {
			Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: original });
		}
	});

	it('keeps a long drawer author accessible while reserving a compact time label', () => {
		const name = 'Canvas Reviewer 1791789535058';
		render(thread({ author: name, createdAt: new Date().toISOString() }));
		const author = host.querySelector<HTMLElement>('.ct-author')!;
		expect(author.getAttribute('aria-label')).toBe(name);
		expect(author.title).toBe(name);
		expect(author.querySelector('.name-tail')?.textContent).toBe('5058');
		expect(host.querySelector('.ct-at.compact')?.textContent).toBe('now');
	});

	it('shows agent comment and reply bylines as author then time only', () => {
		render(thread({
			author: 'conductor', authorKind: 'agent',
			replies: [{ author: 'conductor', authorKind: 'agent', text: 'Reply', at: '1m ago' }],
		}));
		expect(host.querySelector('.ct-author')?.textContent).toBe('Conductor');
		expect(host.querySelector('.ct-msg-author')?.textContent).toBe('Conductor');
		expect(host.textContent).not.toMatch(/via Claude Code|opus-4\.8|project coordination/);
	});

	it('posts a reply on Cmd+Enter', () => {
		const { onReply } = render(thread());
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, 'ship it');
		input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, bubbles: true }));
		expect(onReply).toHaveBeenCalledWith('c1', 'ship it', [], []);
	});

	function visibleObserver() {
		vi.stubGlobal('IntersectionObserver', class {
			constructor(private callback: IntersectionObserverCallback) {}
			observe(target: Element) { this.callback([{ isIntersecting: true, intersectionRatio: 1, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
			disconnect() {}
			unobserve() {}
			takeRecords() { return []; }
			root = null; rootMargin = ''; thresholds = [];
		});
	}

	it('stamps an unread reply only after it is observed in the viewport', async () => {
		visibleObserver();
		const onViewed = vi.fn().mockResolvedValue(undefined);
		render(thread({ newReplyIds: ['r1'], replies: [{ ...reply, id: 'r1', createdAt: '2026-10-16T18:00:00Z' }] }), false, { onViewed, chatSurface: true });
		await vi.waitFor(() => expect(onViewed).toHaveBeenCalledWith('c1', 'r1'), { timeout: 3000 });
		vi.unstubAllGlobals();
	});

	it('retries a failed visible stamp only on the next visibility change', async () => {
		visibleObserver();
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const onViewed = vi.fn().mockRejectedValue(new Error('offline'));
		render(thread({ newReplyIds: ['r1'], replies: [{ ...reply, id: 'r1', createdAt: '2026-10-16T18:00:00Z' }] }), false, { onViewed, chatSurface: true });
		await vi.waitFor(() => expect(warn).toHaveBeenCalled(), { timeout: 3000 });
		flushSync(); await Promise.resolve(); flushSync();
		expect(onViewed).toHaveBeenCalledTimes(1);
		document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); flushSync();
		await vi.waitFor(() => expect(onViewed).toHaveBeenCalledTimes(2), { timeout: 3000 });
		warn.mockRestore(); vi.unstubAllGlobals();
	});

	it('shows only new and latest replies in a long thread until earlier replies are expanded', () => {
		const replies = Array.from({ length: 6 }, (_, index) => ({ ...reply, id: `r${index}`, text: `reply ${index}`, createdAt: `2026-10-16T18:0${index}:00Z` }));
		render(thread({ replies, newReplyIds: ['r4', 'r5'] }));
		expect(host.textContent).toContain('Show 4 earlier replies');
		expect(host.textContent).not.toContain('reply 0');
		expect(host.textContent).toContain('reply 4');
		expect(host.textContent).toContain('reply 5');
		expect(host.querySelectorAll('.ct-new')).toHaveLength(2);
		button('.ct-earlier').click(); flushSync();
		expect(host.textContent).toContain('reply 0');
	});

	it('marks and cuts at the replies the server names, never at the viewer own newest reply', async () => {
		visibleObserver();
		const onViewed = vi.fn().mockResolvedValue(undefined);
		const replies = Array.from({ length: 6 }, (_, index) => ({ ...reply, id: `r${index}`, text: `reply ${index}`, createdAt: `2026-10-16T18:0${index}:00Z` }));
		render(thread({ replies, newReplyIds: [] }), false, { onViewed });
		expect(host.querySelector('.ct-new')).toBeNull();
		// Up to 5 replies show whole; a longer thread with nothing unread shows its last 2.
		expect(host.textContent).toContain('Show 4 earlier replies');
		expect(host.textContent).toContain('reply 5');
		await Promise.resolve(); flushSync();
		expect(onViewed).not.toHaveBeenCalled();
		vi.unstubAllGlobals();
	});

	it('expanding earlier replies does not focus the thread', () => {
		const onFocus = vi.fn();
		const replies = Array.from({ length: 6 }, (_, index) => ({ ...reply, id: `r${index}`, text: `reply ${index}`, createdAt: `2026-10-16T18:0${index}:00Z` }));
		render(thread({ replies }), false, { onFocus });
		button('.ct-earlier').click(); flushSync();
		expect(host.textContent).toContain('reply 0');
		expect(onFocus).not.toHaveBeenCalled();
	});

	it('keeps a reply being typed on a thread someone else resolves, and says so', () => {
		const { onResolve } = render(thread({ resolved: true }), false, { replyText: 'half a reply', kept: true });
		expect(host.querySelector('.ct-res-head')).toBeNull();
		expect((host.querySelector('.ct-input') as HTMLTextAreaElement).value).toBe('half a reply');
		expect(host.querySelector('[data-resolved-note]')?.textContent).toContain('Send adds your reply; the thread stays resolved.');
		expect(host.querySelector('.ct-act.ack')).toBeNull();
		button('[data-resolved-note] .reopen').click();
		expect(onResolve).toHaveBeenCalledWith('c1', false);
	});

	it('keeps a reply started inside a resolved thread in its resolved row, collapsed or not', () => {
		render(thread({ resolved: true }), false, { replyText: 'started after it was resolved' });
		expect(host.querySelector('.ct-res-head')).not.toBeNull();
		expect(host.querySelector('[data-resolved-note]')).toBeNull();
		button('.ct-res-head').click(); flushSync();
		expect((host.querySelector('.ct-input') as HTMLTextAreaElement).value).toBe('started after it was resolved');
		button('.ct-res-head').click(); flushSync();
		expect(host.querySelector('.ct-res-head')).not.toBeNull();
		expect(host.querySelector('[data-resolved-note]')).toBeNull();
	});

	it('collapses a resolved thread with no reply being typed', () => {
		render(thread({ resolved: true }));
		expect(host.querySelector('.ct-res-head')).not.toBeNull();
		expect(host.querySelector('[data-resolved-note]')).toBeNull();
	});

	it('shows no New marks on a thread whose projection names none', () => {
		render(thread({ replies: [{ ...reply, id: 'r1', createdAt: '2026-10-16T18:00:00Z' }] }));
		expect(host.querySelector('.ct-new')).toBeNull();
	});

	it('keeps the reply field available after Agree', () => {
		render(thread({ acked: true, acknowledgedBy: [{ id: 'ui', name: 'Steve', kind: 'user' }] }));
		expect(host.querySelector('.ct-input')).not.toBeNull();
		expect(host.textContent).toContain('Agreed: Steve');
	});

	it('names everyone who agreed, and lights Agree only for the person', () => {
		const infra = { id: 'infra', name: 'Infrastructure', kind: 'agent' as const };
		render(thread({ acked: false, acknowledgedBy: [infra] }));
		expect(host.querySelector('.ct-agreed')?.textContent).toContain('Agreed: Infrastructure');
		expect(button('.ct-act.ack').classList.contains('on')).toBe(false);
		unmount(component!); component = null; host.remove();
		render(thread({ acked: true, acknowledgedBy: [{ id: 'ui', name: 'Steve', kind: 'user' }, infra] }));
		expect(host.querySelector('.ct-agreed')?.textContent).toContain('Agreed: Steve, Infrastructure');
		expect(button('.ct-act.ack').classList.contains('on')).toBe(true);
	});

	it('toggles Agree', () => {
		const { onAgree } = render(thread({ acked: true }));
		button('.ct-act.ack').click();
		expect(onAgree).toHaveBeenCalledWith('c1', false);
		expect(button('.ct-act.ack').getAttribute('aria-label')).toContain('Agree');
	});

	it('resolves an open thread', () => {
		const { onResolve, onAgree } = render(thread());
		button('.ct-act.resolve').click();
		expect(onResolve).toHaveBeenCalledWith('c1', true);
		expect(onAgree).not.toHaveBeenCalled();
	});

	it('re-opens a resolved thread from its expanded summary', () => {
		const { onResolve } = render(thread({ resolved: true, replies: [{ author: 'agent-1', authorKind: 'agent', text: 'done', at: 'later' }] }));
		expect(host.textContent).toContain('Resolved · 2 messages');
		button('.ct-res-head').click(); flushSync();
		button('.ct-link').click();
		expect(onResolve).toHaveBeenCalledWith('c1', false);
	});

	it('says a re-open asks the agent to revisit only when the document has an owner to reach', () => {
		render(thread({ resolved: true }), false, { reopenReachesOwner: true });
		button('.ct-res-head').click(); flushSync();
		expect(host.querySelector('.ct-res-hint')?.textContent).toBe('Re-open asks the agent to revisit.');
		if (component) unmount(component); component = null; host.remove();

		render(thread({ resolved: true }));
		button('.ct-res-head').click(); flushSync();
		expect(host.querySelector('.ct-res-hint')).toBeNull();
		expect(button('.ct-link').textContent).toBe('Re-open');
	});

	it('offers no reply or actions when read-only', () => {
		const { onAgree } = render(thread(), true);
		expect(host.querySelector('.ct-input')).toBeNull();
		button('.ct-act.ack').click();
		expect(onAgree).not.toHaveBeenCalled();
	});

	it('keeps the text-changed fact in the quote instead of duplicating it in a flat row', () => {
		render(thread({ orphaned: true }), false, { flat: true });
		expect(host.querySelector('.ct-orphan')).toBeNull();
		unmount(component!); host.remove();
		render(thread({ orphaned: true }));
		expect(host.querySelector('.ct-orphan')?.textContent).toContain('text this comment was on has changed');
	});

	it('labels a card whose original text is gone with the amber note and keeps its place', () => {
		render(thread({ orphaned: true, detached: true }));
		expect(host.querySelector('.ct-orphan')?.textContent).toBe('This text isn’t in the document');
		expect(host.querySelector('.ct.orphaned')).not.toBeNull();
	});

	it('keeps the card header on one row, the long name shortened in the middle, at rest, focused, in the margin and in the popover or sheet', () => {
		const name = 'Suggestion Reviewer 1793988264642';
		const rows = (extra: Record<string, unknown>) => {
			render(thread({ author: name, createdAt: new Date().toISOString(), replies: [{ ...reply, author: name }] }), false, extra);
			const out = { name: host.querySelector('.ct-author')!.textContent, tail: host.querySelector('.ct-author .name-tail')?.textContent, replyTail: host.querySelector('.ct-msg-author .name-tail')?.textContent, label: host.querySelector('.ct-author')!.getAttribute('aria-label') };
			unmount(component!); component = null; host.remove();
			return out;
		};
		const rest = rows({ inMargin: true, focused: false, scrollOnFocus: false });
		expect(rest).toEqual({ name, tail: '4642', replyTail: '4642', label: name });
		expect(rows({ inMargin: true, focused: true, scrollOnFocus: false })).toEqual(rest);
		expect(rows({ focused: true, scrollOnFocus: false })).toEqual(rest);
		expect(rows({ focused: true, flat: true, scrollOnFocus: false })).toEqual(rest);
	});

	it('lets the person read a long deleted text in full, and clamps it again, with the Show more control chat comments use', () => {
		const long = 'The deleted paragraph. '.repeat(60);
		render(thread({ orphaned: true, detached: true, anchor: long }));
		const more = () => host.querySelector<HTMLButtonElement>('[data-quote-more]')!;
		expect([more().textContent, more().getAttribute('aria-expanded')]).toEqual(['Show more', 'false']);
		expect(host.querySelector<HTMLElement>('.quote-line')!.style.getPropertyValue('--quote-lines')).toBe('1');
		more().click(); flushSync();
		expect([more().textContent, more().getAttribute('aria-expanded')]).toEqual(['Show less', 'true']);
		expect(host.querySelector<HTMLElement>('.quote-line')!.style.getPropertyValue('--quote-lines')).toBe('unset');
	});

	it('shows a short deleted text whole with no control', () => {
		render(thread({ orphaned: true, detached: true, anchor: 'A short deleted sentence.' }));
		expect(host.querySelector('[data-quote-more]')).toBeNull();
		expect(host.querySelector('.quote')?.textContent).toContain('A short deleted sentence.');
	});

	it('renders a suggestion diff inside the thread', () => {
		render(thread({ anchor: 'old text', replacement: 'new text' }));
		expect(host.querySelector('.diff')?.textContent).toContain('old text');
		expect(host.querySelector('.diff')?.textContent).toContain('new text');
	});

	it('keeps the reply field visible on an open suggestion without Agree or Resolve', () => {
		render(thread({ anchor: 'old text', replacement: 'new text' }));
		expect(host.querySelector('textarea[placeholder="Reply…"]')).toBeTruthy();
		expect(host.querySelector('.ct-act.ack')).toBeNull();
		expect(host.querySelector('.ct-act.resolve')).toBeNull();
	});

	it('wraps the suggestion diff between words, on the open and the decided card alike', () => {
		render(thread({ anchor: 'old text', replacement: 'new text' }));
		expect(host.querySelector('.diff')?.classList.contains('words')).toBe(true);
		unmount(component!); host.remove();
		render(thread({ anchor: 'old text', replacement: 'new text', decision: 'rejected', resolved: true }));
		button('.ct-res-head').click(); flushSync();
		expect(host.querySelector('.diff')?.classList.contains('words')).toBe(true);
	});

	it('shows the person tag on a suggestion with no text, open, decided and resolved', () => {
		const chip = () => host.querySelector('[data-person-tag]')?.textContent;
		render(thread({ anchor: 'old text', replacement: 'new text', text: '', taggedPerson: 'steve' }));
		expect(chip()).toBe('@steve');
		unmount(component!); host.remove();
		render(thread({ anchor: 'old text', replacement: 'new text', text: '', taggedPerson: 'steve', decision: 'accepted' }));
		expect(chip()).toBe('@steve');
		unmount(component!); host.remove();
		render(thread({ anchor: 'old text', replacement: 'new text', text: '', taggedPerson: 'steve', decision: 'rejected', resolved: true }));
		button('.ct-res-head').click(); flushSync();
		expect(chip()).toBe('@steve');
	});

	it('gives an agent the avatar colour of its presence caret, and any other author the participant colour', () => {
		render(thread({ author: 'Reviewer', authorKind: 'agent', authorThreadId: 'thread-1' }));
		expect(host.querySelector<HTMLElement>('.avatar')!.style.background).toBe(hexToRgb(agentColor('thread-1')));
		unmount(component!); host.remove();
		render(thread({ author: 'Reviewer', authorKind: 'agent' }));
		expect(host.querySelector<HTMLElement>('.avatar')!.style.background).toBe('var(--text-tertiary)');
	});

	it('disables accepting an orphaned suggestion with the reason', () => {
		render(thread({ replacement: 'new text', orphaned: true }));
		const accept = button('.ct-suggestion-accept');
		expect(accept.disabled).toBe(true);
		expect(accept.title).toBe("Can't accept: the text this suggestion replaces isn't in the document.");
		expect(host.textContent).toContain("The text this suggestion replaces isn't in the document.");
	});

	it('disables accepting a suggestion edited inside, with the reason, while Reject and Reply stay', () => {
		render(thread({ replacement: 'new text', textChanged: true }));
		const accept = button('.ct-suggestion-accept');
		expect(accept.disabled).toBe(true);
		expect(accept.title).toBe("Can't accept: the text doesn't match this suggestion.");
		expect(host.querySelector('.ct-orphan')?.textContent).toBe("The text doesn't match this suggestion.");
		expect(host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.disabled).toBe(false);
		expect(host.querySelector('.ct-input, .ct-compose')).not.toBeNull();
	});

	it('collapses a decided suggestion with attribution', () => {
		render(thread({ replacement: 'new text', decision: 'accepted', decidedBy: 'Ada', resolved: true }));
		expect(host.textContent).toContain('Suggested by You · Accepted by Ada');
		expect(host.querySelector('.ct-suggestion-actions')).toBeNull();
	});

	it('hides suggestion decisions for read-only viewers', () => {
		render(thread({ replacement: 'new text' }), true);
		expect(host.querySelector('.ct-suggestion-actions')).toBeNull();
	});

	it('shows replies and an always-open reply field on a suggestion, without Agree or Resolve', async () => {
		const { onReply } = render(thread({ replacement: 'new text', replies: [reply] }));
		expect(host.querySelector('.ct-replies')).not.toBeNull();
		expect(host.querySelector('.ct-replies')?.textContent).toContain('Could we keep the old wording?');
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, ' fine by me ');
		button('.ct-send').click();
		await vi.waitFor(() => expect(input.value).toBe(''));
		expect(onReply).toHaveBeenCalledWith('c1', 'fine by me', [], []);
		expect(host.querySelector('.ct-act.ack')).toBeNull();
		expect(host.querySelector('.ct-act.resolve')).toBeNull();
		expect(host.querySelector('.ct-suggestion-actions')).not.toBeNull();
	});

	it('shows replies but no reply box on a read-only suggestion', () => {
		render(thread({ replacement: 'new text', replies: [reply] }), true);
		expect(host.querySelector('.ct-input')).toBeNull();
		expect(host.querySelector('.ct-replies')).not.toBeNull();
	});

	it('shows the replies of a decided suggestion when expanded, with no reply box', () => {
		render(thread({ replacement: 'new text', decision: 'rejected', decidedBy: 'Ada', resolved: true, replies: [reply] }));
		expect(host.textContent).not.toContain('Could we keep the old wording?');
		button('.ct-res-head').click(); flushSync();
		expect(host.querySelector('.ct-res-body')?.textContent).toContain('Could we keep the old wording?');
		expect(host.querySelector('.ct-input')).toBeNull();
	});

	it.each([
		['accept', '.ct-suggestion-accept', 'Accepting…', '.ct-suggestion-actions .ct-link', 'Reject'],
		['reject', '.ct-suggestion-actions .ct-link', 'Rejecting…', '.ct-suggestion-accept', 'Accept'],
	] as const)('shows the busy label only on the clicked button while %s is pending', async (decision, clicked, busyLabel, other, otherLabel) => {
		let settle!: () => void;
		const onDecision = vi.fn(() => new Promise<void>((resolve) => { settle = resolve; }));
		render(thread({ replacement: 'new text' }), false, { onDecision });
		button(clicked).click(); flushSync();
		expect(onDecision).toHaveBeenCalledWith('c1', decision);
		expect(button(clicked).textContent).toBe(busyLabel);
		expect(button(other).textContent).toBe(otherLabel);
		expect(button(clicked).disabled).toBe(true);
		expect(button(other).disabled).toBe(true);
		button(other).click(); button(clicked).click();
		expect(onDecision).toHaveBeenCalledOnce();
		settle();
		await vi.waitFor(() => expect(button(clicked).disabled).toBe(false));
		expect(button(other).disabled).toBe(false);
		expect(host.querySelector('.ct-suggestion-actions')?.textContent).not.toMatch(/Accepting|Rejecting/);
	});

	it('shows the refusal it is given, as an alert', () => {
		render(thread({ replacement: 'new text' }), false, { decisionError: 'The text changed on the server' });
		expect(host.querySelector('[role="alert"]')?.textContent).toBe('The text changed on the server');
	});

	it('marks a rejected suggestion with a cross and an accepted one with a check', () => {
		render(thread({ replacement: 'new text', decision: 'rejected', decidedBy: 'Ada', resolved: true }));
		expect(host.textContent).toContain('Suggested by You · Rejected by Ada');
		expect(host.querySelector('.ct-res-head .ct-cross path')?.getAttribute('d')).toBe('M18 6L6 18M6 6l12 12');
		expect(host.querySelector('.ct-res-head .ct-check')).toBeNull();
		unmount(component!); host.remove();
		render(thread({ replacement: 'new text', decision: 'accepted', decidedBy: 'Ada', resolved: true }));
		expect(host.querySelector('.ct-res-head .ct-check')).not.toBeNull();
		expect(host.querySelector('.ct-res-head .ct-cross')).toBeNull();
	});

	it("shows an image comment's number in its tag colour, open and resolved", () => {
		render(thread({ anchor: '', regionNumber: 3 }), false, { tagColor: '#e5484d' });
		const num = host.querySelector<HTMLElement>('.ct-head .ct-num')!;
		expect(num.textContent).toBe('3');
		expect(num.getAttribute('aria-label')).toBe('Comment 3');
		expect(num.style.getPropertyValue('--region-color')).toBe('#e5484d');
		unmount(component!); host.remove();
		render(thread({ anchor: '', regionNumber: 3, resolved: true }));
		expect(host.querySelector('.ct-res-head .ct-num')?.textContent).toBe('3');
	});

	it("fills the drawer's number chip with the viewer disc's stepped colour so the digit reads on it", () => {
		render(thread({ anchor: '', regionNumber: 3 }), false, { tagColor: '#8b5cf6' });
		expect(host.querySelector<HTMLElement>('.ct-head .ct-num')!.style.getPropertyValue('--disc-color')).toBe('#aa87f8');
		unmount(component!); host.remove();
		render(thread({ anchor: '', regionNumber: 3 }));
		expect(host.querySelector<HTMLElement>('.ct-head .ct-num')!.style.getPropertyValue('--disc-color')).toBe('');
	});

	it('says when the diagram changed under a pin, with the same Move pin', () => {
		const onMovePin = vi.fn();
		render(thread({ anchor: '```mermaid', regionNumber: 1, regionStale: true }), false, { onMovePin, diagram: true });
		const note = host.querySelector('[data-region-stale]')?.textContent ?? '';
		expect(note).toContain('Diagram changed since this comment');
		expect(note).not.toContain('Image');
		button('[data-move-pin]').click();
		expect(onMovePin).toHaveBeenCalledWith('c1');
	});

	it('says when the image changed under a pin and offers Move pin', () => {
		const onMovePin = vi.fn();
		render(thread({ anchor: '', regionNumber: 1, regionStale: true }), false, { onMovePin });
		expect(host.querySelector('[data-region-stale]')?.textContent).toContain('Image changed since this comment');
		button('[data-move-pin]').click();
		expect(onMovePin).toHaveBeenCalledWith('c1');
		unmount(component!); host.remove();
		render(thread({ anchor: '', regionNumber: 1, regionStale: true }), true, { onMovePin });
		expect(host.querySelector('[data-move-pin]')).toBeNull();
	});
});

describe('CommentComposer', () => {
	it('names a region instead of quoting text', () => {
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(CommentComposer, { target: host, props: { quote: '', label: 'Box on the image', onSubmit: vi.fn(), onCancel: vi.fn() } });
		flushSync();
		expect(host.querySelector('.quote')?.textContent).toBe('Box on the image');
	});

	it('submits trimmed text and cancels on Escape', async () => {
		host = document.createElement('div'); document.body.appendChild(host);
		const onSubmit = vi.fn().mockResolvedValue(undefined); const onCancel = vi.fn();
		component = mount(CommentComposer, { target: host, props: { quote: 'the quoted text', onSubmit, onCancel } });
		flushSync();
		const textarea = host.querySelector<HTMLTextAreaElement>('textarea')!;
		type(textarea, ' first point ');
		button('.submit').click();
		await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith('first point', [], []));
		textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		expect(onCancel).toHaveBeenCalledOnce();
	});

	it('keeps the draft when the submit fails so it can be retried', async () => {
		host = document.createElement('div'); document.body.appendChild(host);
		const onSubmit = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined);
		component = mount(CommentComposer, { target: host, props: { quote: 'the quoted text', onSubmit, onCancel: vi.fn() } });
		flushSync();
		const textarea = host.querySelector<HTMLTextAreaElement>('textarea')!;
		type(textarea, 'first point');
		button('.submit').click();
		flushSync();
		expect(button('.submit').textContent).toBe('Posting…');
		await vi.waitFor(() => { flushSync(); expect(button('.submit').textContent).toBe('Comment'); });
		expect(textarea.value).toBe('first point');
		button('.submit').click();
		await vi.waitFor(() => expect(textarea.value).toBe(''));
		expect(onSubmit).toHaveBeenCalledTimes(2);
	});
});

describe('a comment whose text was deleted', () => {
	it('says the original text was deleted and keeps the old text in quotes', () => {
		render(thread({ anchor: 'two extra liters', orphaned: true, detached: true }));
		expect(host.querySelector('.ct-orphan')?.textContent).toBe('This text isn’t in the document');
		expect(host.textContent).toContain('two extra liters');
	});

	it('keeps the changed-text wording when the text is still in the document but no longer matches', () => {
		render(thread({ orphaned: true, detached: false }));
		expect(host.querySelector('.ct-orphan')?.textContent).toBe('The text this comment was on has changed');
	});
});

describe('a decided suggestion that has replies', () => {
	const accepted = (overrides: Partial<CommentThreadData> = {}) => thread({ replacement: 'ten', decision: 'accepted', author: 'Reviewer', text: 'Nine was a typo', replies: [reply], ...overrides });

	it('reads as decided: one status line, the conversation, no diff and no Accept or Reject', () => {
		render(accepted());
		expect(host.querySelector('.ct-decided')?.textContent).toContain('Suggestion accepted');
		expect(host.textContent).toContain('Nine was a typo');
		expect(host.textContent).toContain(reply.text);
		expect(host.querySelector('.ct-suggestion-accept')).toBeNull();
		expect(host.querySelector('.ct-suggestion-actions')).toBeNull();
		expect(host.textContent).not.toContain('Suggested edit');
	});

	it('has a reply box and a Resolve button that resolves the thread', async () => {
		const { onResolve, onReply } = render(accepted(), false, { showReply: true });
		await vi.waitFor(() => expect(host.querySelector('.ct-input')).not.toBeNull());
		type(host.querySelector<HTMLInputElement>('.ct-input')!, 'Thanks');
		button('.ct-send').click();
		await vi.waitFor(() => expect(onReply).toHaveBeenCalledWith('c1', 'Thanks', [], []));
		button('.ct-act.resolve').click();
		expect(onResolve).toHaveBeenCalledWith('c1', true);
	});

	it('shows a rejected one the same way', () => {
		render(accepted({ decision: 'rejected' }));
		expect(host.querySelector('.ct-decided')?.textContent).toContain('Suggestion rejected');
	});

	it('is the collapsed decided row, with no reply box, once it is resolved', () => {
		render(accepted({ resolved: true }));
		expect(host.querySelector('.ct.resolved')).not.toBeNull();
		expect(host.querySelector('.ct-decided')).toBeNull();
	});
});
