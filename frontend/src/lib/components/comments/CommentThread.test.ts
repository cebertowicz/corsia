import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread, { type CommentThreadData } from './CommentThread.svelte';
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
	it('posts a reply and clears the input', async () => {
		const { onReply } = render(thread());
		button('.ct-reply-open').click(); flushSync();
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, '  looks right  ');
		button('.ct-send').click();
		await vi.waitFor(() => expect(input.value).toBe(''));
		expect(onReply).toHaveBeenCalledWith('c1', 'looks right');
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
		button('.ct-reply-open').click(); flushSync();
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, 'ship it');
		input.setSelectionRange(1, 4);
		input.dispatchEvent(new Event('select', { bubbles: true }));
		button('.ct-send').click();
		await vi.waitFor(() => expect(onReply).toHaveBeenCalledWith('c1', 'ship it'));
		await vi.waitFor(() => expect(onReplySelection).toHaveBeenLastCalledWith(null));
	});

	it('clears the owned selection when the draft is emptied by hand', () => {
		const onReplySelection = vi.fn();
		render(thread(), false, { onReplySelection });
		button('.ct-reply-open').click(); flushSync();
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, 'draft');
		expect(onReplySelection).toHaveBeenLastCalledWith([5, 5]);
		type(input, '');
		expect(onReplySelection).toHaveBeenLastCalledWith(null);
	});

	it('reveals the reply field only after Reply is pressed', () => {
		render(thread());
		expect(host.querySelector('.ct-input')).toBeNull();
		button('.ct-reply-open').click(); flushSync();
		expect(host.querySelector('.ct-compose.open .ct-input')).not.toBeNull();
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

	it('posts a reply on Cmd+Enter', () => {
		const { onReply } = render(thread());
		button('.ct-reply-open').click(); flushSync();
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, 'ship it');
		input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, bubbles: true }));
		expect(onReply).toHaveBeenCalledWith('c1', 'ship it');
	});

	it('stamps the newest rendered reply only after the thread expands', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		render(thread({ replyWaiting: true, replies: [{ ...reply, id: 'r1' }] }), false, { onViewed });
		expect(onViewed).not.toHaveBeenCalled();
		button('.ct-replies-toggle').click(); flushSync();
		await vi.waitFor(() => expect(onViewed).toHaveBeenCalledWith('c1', 'r1'));
	});

	it('retries a failed stamp only on the next visibility change', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const onViewed = vi.fn().mockRejectedValue(new Error('offline'));
		render(thread({ replyWaiting: true, replies: [{ ...reply, id: 'r1' }] }), false, { onViewed });
		button('.ct-replies-toggle').click(); flushSync();
		await vi.waitFor(() => expect(warn).toHaveBeenCalled());
		flushSync(); await Promise.resolve(); flushSync();
		expect(onViewed).toHaveBeenCalledTimes(1);
		document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); flushSync();
		await vi.waitFor(() => expect(onViewed).toHaveBeenCalledTimes(2));
		warn.mockRestore();
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

	it('renders a suggestion diff inside the thread', () => {
		render(thread({ anchor: 'old text', replacement: 'new text' }));
		expect(host.querySelector('.diff')?.textContent).toContain('old text');
		expect(host.querySelector('.diff')?.textContent).toContain('new text');
	});

	it('keeps the Reply action on an open suggestion, which is how a touch screen reaches its reply field', () => {
		render(thread({ anchor: 'old text', replacement: 'new text' }));
		const reply = host.querySelector<HTMLButtonElement>('button.ct-act.reply[aria-label="Reply"]')!;
		expect(reply).toBeTruthy();
		reply.click(); flushSync();
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
		expect(accept.title).toBe('The text this suggestion replaces has changed.');
		expect(host.textContent).toContain('The text this suggestion replaces has changed.');
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

	it('shows the replies on an open suggestion and takes a new one, without Agree or Resolve', async () => {
		const { onReply } = render(thread({ replacement: 'new text', replies: [reply] }));
		expect(host.querySelector('.ct-replies')).toBeNull();
		button('.ct-replies-toggle').click(); flushSync();
		expect(host.querySelector('.ct-replies')?.textContent).toContain('Could we keep the old wording?');
		button('.ct-reply-open').click(); flushSync();
		const input = host.querySelector<HTMLInputElement>('.ct-input')!;
		type(input, ' fine by me ');
		button('.ct-send').click();
		await vi.waitFor(() => expect(input.value).toBe(''));
		expect(onReply).toHaveBeenCalledWith('c1', 'fine by me');
		expect(host.querySelector('.ct-act.ack')).toBeNull();
		expect(host.querySelector('.ct-act.resolve')).toBeNull();
		expect(host.querySelector('.ct-suggestion-actions')).not.toBeNull();
	});

	it('offers no reply box on a suggestion to read-only viewers', () => {
		render(thread({ replacement: 'new text', replies: [reply] }), true);
		expect(host.querySelector('.ct-input')).toBeNull();
		expect(host.querySelector('.ct-replies-toggle')).not.toBeNull();
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
		await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith('first point'));
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
