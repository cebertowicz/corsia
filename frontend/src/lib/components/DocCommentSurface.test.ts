import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';

const mocks = vi.hoisted(() => ({
	decideSuggestion: vi.fn(), flushDraft: vi.fn(), seedDraft: vi.fn(), loadComments: vi.fn(),
}));
vi.mock('$lib/api/commentSuggestions', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/api/commentSuggestions')>(), decideSuggestion: mocks.decideSuggestion,
}));
vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: mocks.flushDraft, seedDraft: mocks.seedDraft }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: mocks.loadComments }));

import DocCommentSurface from './DocCommentSurface.svelte';
import { DocFeedback } from '$lib/docFeedback.svelte';
import { SuggestionRefused } from '$lib/api/commentSuggestions';
import { suggestionRefusals } from '$lib/stores/suggestionRefusals';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const target = { instanceId: 'tab-1', path: 'notes.md', name: 'Notes', projectId: 'project-1', threadId: 'thread-1', artifactId: 'artifact-1' };
const suggestion = {
	id: 'comment-1', anchor: 'old text', author: 'agent-1', authorKind: 'agent' as const,
	text: 'Prefer this wording', at: 'now', resolved: false, replacement: 'new text', replies: [],
};
const secondSuggestion = { ...suggestion, id: 'comment-2', anchor: 'other text' };
function render(overrides: Record<string, unknown> = {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	const onSuggestionAccepted = vi.fn(); const onSuggestionBusy = vi.fn();
	component = mount(DocCommentSurface, {
		target: host,
		// Descriptors, not a spread, so a test can pass a live getter for a prop that changes.
		props: Object.defineProperties({
			state: { kind: 'thread', id: suggestion.id }, anchor: document.createElement('button'), narrow: false,
			comments: [suggestion], focusedId: null, draft: null, onClose: vi.fn(), restoreFocus: vi.fn(),
			onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(),
			target, artifactId: 'artifact-1', onSuggestionAccepted, onSuggestionBusy,
		}, Object.getOwnPropertyDescriptors(overrides)),
	});
	flushSync(); return { onSuggestionAccepted, onSuggestionBusy };
}
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	suggestionRefusals.set({});
	vi.clearAllMocks();
});

describe('DocCommentSurface suggestions', () => {
	it('lists all comments in a canvas region with resolved/open filters and text navigation', () => {
		const onFocus = vi.fn();
		render({ state: { kind: 'list' }, comments: [suggestion, { ...secondSuggestion, resolved: true }], onFocus });
		const drawer = host.querySelector('[data-doc-comments-drawer]');
		expect(drawer).not.toBeNull();
		expect(host.querySelector('[data-popover]')).toBeNull();
		const quote = drawer!.querySelector<HTMLButtonElement>('[data-comment-jump]');
		quote!.click();
		expect(onFocus).toHaveBeenCalledWith(suggestion.id);
		drawer!.querySelector<HTMLButtonElement>('[data-drawer-filter]')!.click(); flushSync();
		const resolved = [...drawer!.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Resolved');
		resolved!.click(); flushSync();
		expect(drawer!.querySelectorAll('[data-comment-id]')).toHaveLength(1);
		expect(drawer!.querySelector('[data-comment-id]')?.getAttribute('data-comment-id')).toBe('comment-2');
	});

	it('opens on Open and switches filters from the named menu', () => {
		render({ state: { kind: 'list' }, comments: [suggestion, { ...secondSuggestion, resolved: true }] });
		const drawer = host.querySelector<HTMLElement>('[data-doc-comments-drawer]')!;
		expect(drawer.querySelector('.drawer-heading')?.textContent).toContain('1 open');
		const menu = drawer.querySelector<HTMLButtonElement>('[data-drawer-filter]')!;
		expect(menu.textContent?.trim()).toBe('Filter');
		expect(menu.querySelector('svg')).not.toBeNull();
		menu.click(); flushSync();
		const resolved = [...drawer.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')].find((button) => button.textContent === 'Resolved')!;
		resolved.click(); flushSync();
		expect(drawer.querySelector('.drawer-heading')?.textContent).toContain('1 resolved');
		expect(drawer.querySelector('[data-comment-id]')?.getAttribute('data-comment-id')).toBe('comment-2');
	});

	it('offers the open comments when the resolved filter has none', () => {
		render({ state: { kind: 'list' }, comments: [suggestion] });
		const drawer = host.querySelector<HTMLElement>('[data-doc-comments-drawer]')!;
		drawer.querySelector<HTMLButtonElement>('[data-drawer-filter]')!.click(); flushSync();
		[...drawer.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')].find((button) => button.textContent === 'Resolved')!.click(); flushSync();
		const showOpen = [...drawer.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent?.trim() === 'Show open · 1')!;
		expect(showOpen).not.toBeNull();
		showOpen.click(); flushSync();
		expect(drawer.querySelector('.drawer-heading')?.textContent).toContain('1 open');
		expect(drawer.querySelector('[data-comment-id]')?.getAttribute('data-comment-id')).toBe('comment-1');
	});

	it('ends the drawer in a Send now footer only while messages are not sent', async () => {
		// The open document's one batch: the footer reads it and Send now is its own Send.
		const feedback = new DocFeedback();
		const send = vi.fn(async () => {});
		feedback.send = send;
		feedback.pendingCount = 2;
		feedback.feedback = { open_count: 2, owner_label: 'Style', grace_closes_at: null, undelivered_batches: [] };
		render({ state: { kind: 'list' }, comments: [suggestion], feedback });
		const footer = () => host.querySelector<HTMLElement>('[data-doc-comments-drawer] > [data-drawer-unsent]');
		expect(footer()?.querySelector('[role="status"]')?.textContent).toBe('2 messages not sent yet · they go to Style when you leave.');
		const owner = footer()!.querySelector<HTMLElement>('.drawer-footer-owner')!;
		expect(owner.textContent).toBe('Style');
		expect(owner.title).toBe('Style');
		const button = footer()!.querySelector<HTMLButtonElement>('button')!;
		expect(button.textContent?.trim()).toBe('Send now');
		button.click();
		expect(send).toHaveBeenCalledOnce();
		// The drawer's last child, after the scrolling list, so the list scrolls and the footer stays.
		expect(host.querySelector('[data-doc-comments-drawer]')!.lastElementChild).toBe(footer());

		feedback.countdownNow = Date.parse('2026-10-11T22:00:00Z');
		feedback.feedback = { open_count: 1, owner_label: null, grace_closes_at: '2026-10-11T22:04:00Z', undelivered_batches: [] };
		feedback.pendingCount = 1;
		feedback.sending = true; flushSync();
		expect(footer()?.querySelector('[role="status"]')?.textContent).toBe('1 message not sent yet · sending to the owner in 4 min');
		expect(footer()!.querySelector('button')!.disabled).toBe(true);

		// Sending from the keyboard: the footer leaves with focus in it, which lands on the drawer's heading, not the page.
		feedback.sending = false; flushSync();
		footer()!.querySelector('button')!.focus();
		feedback.pendingCount = 0; flushSync();
		expect(footer()).toBeNull();
		await Promise.resolve();
		expect(document.activeElement).toBe(host.querySelector('[data-drawer-focus-home]'));
	});

	it('keeps the drawer while an image opened from a card covers it, then gives that card focus back', async () => {
		const covering = writable(false);
		const live = fromStore(covering);
		render({ state: { kind: 'list' }, comments: [suggestion, secondSuggestion], get covered() { return live.current; } });
		await tick();
		const card = host.querySelector<HTMLElement>('[data-comment-id="comment-2"]')!;
		const control = card.querySelector<HTMLButtonElement>('button:not(:disabled)')!;
		// A click on the card's text focuses nothing; what it opened from is still known.
		card.click();
		covering.set(true); flushSync();
		expect(host.querySelector('[data-doc-comments-drawer]')).not.toBeNull();
		// The image over the document held focus, and it went to the page as the image closed.
		(document.activeElement as HTMLElement | null)?.blur();
		expect(document.activeElement).toBe(document.body);
		covering.set(false); flushSync();
		await tick();
		expect(document.activeElement).toBe(control);
	});

	it('leaves focus alone when the drawer is uncovered and nothing in it opened the image', async () => {
		const covering = writable(false);
		const live = fromStore(covering);
		render({ state: { kind: 'list' }, comments: [suggestion], get covered() { return live.current; } });
		// The drawer puts focus on its filter as it opens; the image was opened from outside it, which took focus.
		await tick();
		(document.activeElement as HTMLElement | null)?.blur();
		covering.set(true); flushSync();
		covering.set(false); flushSync();
		await tick();
		expect(document.activeElement).toBe(document.body);
	});

	it('closes a card that follows its text only once that comment is deleted', () => {
		const reading = () => new DOMRect(520, 100, 20, 20);
		const gone = vi.fn();
		render({ anchor: reading, comments: [], onClose: gone });
		expect(gone).toHaveBeenCalledOnce();
		unmount(component!); host.remove(); component = null;
		for (const comments of [[{ ...suggestion, resolved: true }], [{ ...suggestion, orphaned: true }], [suggestion]]) {
			const onClose = vi.fn();
			render({ anchor: reading, comments, onClose });
			expect(onClose).not.toHaveBeenCalled();
			unmount(component!); host.remove(); component = null;
		}
	});

	it('keeps a document text-comment sheet above the keyboard on narrow screens', () => {
		render({ narrow: true, state: { kind: 'thread', id: suggestion.id } });
		expect(document.body.querySelector('.drawer-backdrop')?.classList.contains('follow-keyboard')).toBe(true);
	});

	it('removes its narrow drawer synchronously when its caller is destroyed', () => {
		render({ narrow: true, state: { kind: 'thread', id: suggestion.id } });
		expect(document.body.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(1);
		unmount(component!);
		component = null;
		expect(document.body.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);
	});

	describe('a card that follows its text when the comment stops having a place in it', () => {
		// Where the text is drawn until it is not, then nothing: what the editor reports once the comment is orphaned.
		let drawn: DOMRect | null;
		const reading = () => drawn;
		const popover = () => host.querySelector<HTMLElement>('[data-popover]')!;
		const placed = () => ({ left: popover().style.left, top: popover().style.top, hidden: popover().style.visibility === 'hidden' });
		const stateOf = (comment: Record<string, unknown>) => {
			const comments = writable([comment]);
			return { comments, set: (next: Record<string, unknown>) => { comments.set([next]); flushSync(); } };
		};
		function renderFollowing(comment: Record<string, unknown>, extra: Record<string, unknown> = {}) {
			drawn = new DOMRect(520, 100, 20, 20);
			const { comments, set } = stateOf(comment);
			const live = fromStore(comments);
			const onClose = vi.fn();
			render({ anchor: reading, onClose, get comments() { return live.current; }, ...extra });
			return { set, onClose };
		}

		it('keeps a refused Accept open at its place with the reason showing', async () => {
			mocks.flushDraft.mockResolvedValue(true);
			mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused('The text changed on the server', 409, 'stale'));
			const refused = fromStore(suggestionRefusals);
			drawn = new DOMRect(520, 100, 20, 20);
			const onClose = vi.fn();
			render({ anchor: reading, onClose, get comments() { return [{ ...suggestion, orphaned: 'comment-1' in (refused.current['artifact-1'] ?? {}) }]; } });
			await vi.waitFor(() => expect(popover().style.left).toBe('548px'));
			const before = placed();
			// The editor stops drawing the text once the suggestion is refused as stale.
			const accept = host.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!;
			drawn = null;
			accept.click();
			await vi.waitFor(() => expect(host.querySelector('.ct-orphan')?.textContent).toBe('The text this suggestion replaces has changed.'));
			expect(host.querySelector('[role="alert"]')?.textContent).toBe('The text changed on the server');
			await vi.waitFor(() => expect(placed()).toEqual(before));
			expect(onClose).not.toHaveBeenCalled();
		});

		it('keeps the card, its place and a reply being typed when a collaborator orphans the comment', async () => {
			const { set, onClose } = renderFollowing({ ...suggestion, replacement: null });
			await vi.waitFor(() => expect(popover().style.left).toBe('548px'));
			const before = placed();
			host.querySelector<HTMLButtonElement>('.ct-reply-open')!.click(); flushSync();
			const input = host.querySelector<HTMLInputElement>('.ct-input')!;
			input.value = 'A half-written reply';
			input.dispatchEvent(new Event('input', { bubbles: true }));
			flushSync();
			drawn = null;
			set({ ...suggestion, replacement: null, orphaned: true });
			await vi.waitFor(() => expect(host.querySelector('.ct-orphan')).not.toBeNull());
			expect(host.querySelector<HTMLInputElement>('.ct-input')?.value).toBe('A half-written reply');
			expect(placed()).toEqual(before);
			expect(onClose).not.toHaveBeenCalled();
		});

		it('keeps the card at its place when a collaborator resolves the comment', async () => {
			const { set, onClose } = renderFollowing({ ...suggestion, replacement: null });
			await vi.waitFor(() => expect(popover().style.left).toBe('548px'));
			const before = placed();
			drawn = null;
			set({ ...suggestion, replacement: null, resolved: true });
			await vi.waitFor(() => expect(host.querySelector('summary')?.textContent).toContain('Resolved'));
			expect(placed()).toEqual(before);
			expect(onClose).not.toHaveBeenCalled();
		});

		it('still closes when the comment is deleted', async () => {
			const { comments } = stateOf({ ...suggestion });
			const live = fromStore(comments);
			drawn = new DOMRect(520, 100, 20, 20);
			const onClose = vi.fn();
			render({ anchor: reading, onClose, get comments() { return live.current; } });
			comments.set([]); flushSync();
			expect(onClose).toHaveBeenCalledOnce();
		});
	});

	it('keeps loading and error states truthful and delegates Retry', () => {
		const onRetry = vi.fn();
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		render({ state: { kind: 'list' }, comments: [], load: { state: 'error', error: 'Failed to fetch' }, onRetry });
		// It says what to do; the raw transport error goes to the log.
		expect(host.textContent).toContain("Couldn't load comments. Check the connection and retry.");
		expect(host.textContent).not.toContain('Failed to fetch');
		expect(warn).toHaveBeenCalledWith('Could not load comments', 'Failed to fetch');
		warn.mockRestore();
		expect(host.textContent).not.toContain('No comments yet.');
		[...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry')!.click();
		expect(onRetry).toHaveBeenCalledOnce();
	});
	it('opens Reply for the selected card in a narrow canvas drawer', async () => {
		const original = HTMLElement.prototype.scrollIntoView;
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
		try {
			render({ state: { kind: 'list' }, narrow: true, focusedId: suggestion.id, comments: [{ ...suggestion, replacement: null }] });
			await vi.waitFor(() => expect(document.querySelector<HTMLInputElement>('.ct-input')).not.toBeNull());
			expect(document.activeElement?.classList.contains('ct-input')).not.toBe(true);
		} finally {
			Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: original });
		}
	});
	it('focuses Reply without turning input clicks into text navigation', async () => {
		const onFocus = vi.fn();
		render({ state: { kind: 'list' }, comments: [{ ...suggestion, replacement: null }], onFocus });
		host.querySelector<HTMLButtonElement>('.ct-reply-open')!.click();
		await vi.waitFor(() => expect(document.activeElement?.classList.contains('ct-input')).toBe(true));
		host.querySelector<HTMLInputElement>('.ct-input')!.click();
		expect(onFocus).not.toHaveBeenCalled();
	});
	it('consumes drawer Escape once and restores its opener without reaching the document', () => {
		const onClose = vi.fn(), restoreFocus = vi.fn(), documentEscape = vi.fn();
		render({ state: { kind: 'list' }, onClose, restoreFocus });
		window.addEventListener('keydown', documentEscape);
		try {
			host.querySelector('[data-drawer-filter]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
			expect(onClose).toHaveBeenCalledOnce();
			expect(restoreFocus).toHaveBeenCalledOnce();
			expect(documentEscape).not.toHaveBeenCalled();
		} finally { window.removeEventListener('keydown', documentEscape); }
	});

	it('keeps the comments sheet on screen through its outro however the surface closes, then removes it once', async () => {
		const state = writable<{ kind: 'list' } | null>(null);
		const live = fromStore(state);
		const onClose = vi.fn(() => state.set(null));
		render({ get state() { return live.current; }, narrow: true, comments: [{ ...suggestion, replacement: null }], onClose });
		const sheets = () => document.body.querySelectorAll('[data-adaptive-drawer] [data-doc-comments-drawer]');
		const close = {
			header: () => document.body.querySelector<HTMLButtonElement>('button[aria-label="Close comments"]')!.click(),
			// The image viewer's Escape, in the capture phase, clears the state without going through the sheet.
			outside: () => state.set(null),
		};
		for (const [path, run] of Object.entries(close)) {
			onClose.mockClear();
			state.set({ kind: 'list' }); flushSync();
			expect(sheets(), path).toHaveLength(1);
			run(); flushSync();
			// Still mounted, rows and all, while the drawer plays its outro.
			expect(sheets(), path).toHaveLength(1);
			expect(document.body.querySelector('[data-doc-comments-drawer] [data-comment-id="comment-1"]'), path).not.toBeNull();
			await vi.waitFor(() => expect(sheets(), path).toHaveLength(0));
			// The outro's end does not close the surface a second time.
			expect(onClose, path).toHaveBeenCalledTimes(path === 'header' ? 1 : 0);
		}
	});

	it('flushes, applies, re-seeds and reloads after accept', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockResolvedValue({ comment: suggestion, text: 'new document', revision: 'r2' });
		mocks.loadComments.mockResolvedValue(undefined);
		const { onSuggestionAccepted, onSuggestionBusy } = render();
		host.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
		await vi.waitFor(() => expect(mocks.loadComments).toHaveBeenCalledWith('artifact-1'));
		expect(mocks.flushDraft).toHaveBeenCalledWith(target);
		expect(mocks.decideSuggestion).toHaveBeenCalledWith('comment-1', 'accept');
		expect(mocks.seedDraft).toHaveBeenCalledWith(target, 'new document', 'r2');
		expect(onSuggestionAccepted).toHaveBeenCalledWith('new document');
		// An Accept names its suggestion, so the editor can mark that one busy.
		expect(onSuggestionBusy).toHaveBeenCalledWith(true, 'comment-1');
		expect(onSuggestionBusy).toHaveBeenLastCalledWith(false);
	});

	it('shows a 409 reason and leaves the suggestion open', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockRejectedValue(new Error('The text changed on the server'));
		const { onSuggestionAccepted } = render();
		host.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('The text changed on the server'));
		expect(host.querySelector('.ct-suggestion-actions')).not.toBeNull();
		expect(onSuggestionAccepted).not.toHaveBeenCalled();
	});

	it('records a refused accept for its card and keeps the reason on the popover card that moves groups, without reloading', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused('The text changed on the server', 409, 'stale'));
		// As DocViewer lists them: a refused card shows as orphaned (commentsInDocumentOrder).
		const refused = fromStore(suggestionRefusals);
		const { onSuggestionAccepted } = render({ get comments() { return [{ ...suggestion, orphaned: 'comment-1' in (refused.current['artifact-1'] ?? {}) }]; } });
		const card = () => host.querySelector<HTMLElement>('.ct')!;
		const accept = () => host.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!;
		const before = card();
		expect(host.querySelector('h3')?.textContent).toBe('Open comments');
		expect(accept().disabled).toBe(false);
		accept().click();
		await vi.waitFor(() => expect(host.querySelector('h3')?.textContent).toBe('Text changed'));
		expect(host.querySelector('[role="alert"]')?.textContent).toBe('The text changed on the server');
		expect(get(suggestionRefusals)).toEqual({ 'artifact-1': { 'comment-1': 'The text changed on the server' } });
		// The popover lists by group, so the card that turned orphaned is a new one.
		expect(card()).not.toBe(before);
		// The refusal is the card's state; nothing has to come back from the server to show it.
		expect(mocks.loadComments).not.toHaveBeenCalled();
		expect(accept().textContent).toBe('Accept');
		expect(accept().disabled).toBe(true);
		expect(host.querySelector('.ct-orphan')?.textContent).toBe('The text this suggestion replaces has changed.');
		expect(onSuggestionAccepted).not.toHaveBeenCalled();
	});

	// The server's kind says what changed: 409 for a state conflict, 403 for an authorization refusal.
	// Only a stale quote is a refusal the card keeps.
	it.each([
		{ kind: 'decided', status: 409, then: 'reloads comments', message: 'This suggestion was already rejected', loads: 1, reads: 0 },
		{ kind: 'unrecorded', status: 409, then: 'reads the document again', message: 'The edit was applied to the document, but the decision could not be recorded', loads: 0, reads: 1 },
		{ kind: 'not_owner', status: 403, then: 'reloads nothing', message: 'Only the owner can decide', loads: 0, reads: 0 },
	])('a $kind $status shows its message, records no refusal and $then', async ({ kind, status, message, loads, reads }) => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused(message, status, kind));
		mocks.loadComments.mockResolvedValue(undefined);
		// The viewer reads the document and then its comments again, so the surface does not load them itself.
		const onDocumentChanged = vi.fn();
		render({ onDocumentChanged });
		host.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toBe(message));
		expect(mocks.loadComments).toHaveBeenCalledTimes(loads);
		expect(onDocumentChanged).toHaveBeenCalledTimes(reads);
		expect(get(suggestionRefusals)).toEqual({});
	});

	it('clears a refusal when the next decision on that suggestion succeeds', async () => {
		mocks.decideSuggestion.mockRejectedValueOnce(new Error('Only the owner can decide'));
		mocks.loadComments.mockResolvedValue(undefined);
		const { onSuggestionBusy } = render();
		const reject = () => host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!;
		reject().click();
		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')).not.toBeNull());
		mocks.decideSuggestion.mockResolvedValue({ comment: suggestion });
		reject().click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalledTimes(2));
		await vi.waitFor(() => expect(reject().disabled).toBe(false));
		expect(host.querySelector('[role="alert"]')).toBeNull();
		expect(onSuggestionBusy).toHaveBeenCalledWith(true, undefined); // a Reject names no suggestion to mark busy
	});

	it('drops a refusal when the document changes', async () => {
		mocks.decideSuggestion.mockRejectedValue(new Error('Only the owner can decide'));
		const artifact = writable('artifact-1');
		const live = fromStore(artifact);
		render({ get artifactId() { return live.current; } });
		host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')).not.toBeNull());
		artifact.set('artifact-2'); flushSync();
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});

	it('ends a refused accept when the surface closes or the document changes', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused('The text changed on the server', 409, 'stale'));
		const state = writable<{ kind: 'thread'; id: string } | null>({ kind: 'thread', id: suggestion.id });
		const artifact = writable('artifact-1');
		const liveState = fromStore(state), liveArtifact = fromStore(artifact);
		render({ get state() { return liveState.current; }, get artifactId() { return liveArtifact.current; } });
		const refuse = async () => {
			host.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
			await vi.waitFor(() => expect(get(suggestionRefusals)['artifact-1']).toEqual({ 'comment-1': 'The text changed on the server' }));
		};
		await refuse();
		state.set(null); flushSync();
		expect(get(suggestionRefusals)).toEqual({});
		state.set({ kind: 'thread', id: suggestion.id }); flushSync();
		expect(host.querySelector('[role="alert"]')).toBeNull();
		await refuse();
		artifact.set('artifact-2'); flushSync();
		expect(get(suggestionRefusals)).toEqual({});
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});

	it('does not reload comments after a failed reject, and keeps the refusal on a card that never orphaned', async () => {
		mocks.decideSuggestion.mockRejectedValue(new Error('Network down'));
		const comments = writable([suggestion]);
		const live = fromStore(comments);
		render({ get comments() { return live.current; } });
		host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toBe('Network down'));
		// Reject never saves the draft first, so a reload here would read positions for text the screen no longer has.
		expect(mocks.flushDraft).not.toHaveBeenCalled();
		expect(mocks.loadComments).not.toHaveBeenCalled();
		// Comments refreshed from elsewhere: the card was never orphaned, so its refusal stays.
		comments.set([{ ...suggestion }]); flushSync();
		expect(host.querySelector('[role="alert"]')?.textContent).toBe('Network down');
	});

	it('drops a refusal once the surface closes', async () => {
		mocks.decideSuggestion.mockRejectedValue(new Error('Only the owner can decide'));
		const state = writable<{ kind: 'thread'; id: string } | null>({ kind: 'thread', id: suggestion.id });
		const live = fromStore(state);
		render({ get state() { return live.current; } });
		host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')).not.toBeNull());
		state.set(null); flushSync();
		state.set({ kind: 'thread', id: suggestion.id }); flushSync();
		expect(host.querySelector('.ct-suggestion-actions')).not.toBeNull();
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});

	it('shows accept-all only when an author has at least two open suggestions', () => {
		render({ state: { kind: 'list' }, comments: [suggestion] });
		expect(host.textContent).not.toContain('Accept all from agent-1');
	});

	it('accepts all, re-seeds without a stale banner, and shows skipped reasons', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockResolvedValue({
			accepted: ['comment-1'],
			skipped: [{ id: 'comment-2', reason: 'overlaps an earlier suggestion' }],
			text: 'accepted document',
			revision: 'r3',
		});
		mocks.loadComments.mockResolvedValue(undefined);
		const { onSuggestionAccepted } = render({
			state: { kind: 'list' },
			comments: [suggestion, secondSuggestion],
		});
		const button = Array.from(host.querySelectorAll('button')).find(
			(candidate) => candidate.textContent === 'Accept all from agent-1',
		) as HTMLButtonElement;
		button.click();
		await vi.waitFor(() => expect(mocks.loadComments).toHaveBeenCalledWith('artifact-1'));
		expect(mocks.decideSuggestion).toHaveBeenCalledWith('comment-1', 'accept', true);
		expect(mocks.seedDraft).toHaveBeenCalledWith(target, 'accepted document', 'r3');
		expect(onSuggestionAccepted).toHaveBeenCalledWith('accepted document');
		expect(host.textContent).toContain('1 skipped');
		expect(host.textContent).toContain('overlaps an earlier suggestion');
		expect(host.textContent).not.toContain('Updated elsewhere');
	});

	it('runs accept-all once, shows it busy, and reports a failure inline', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		let fail!: (error: Error) => void;
		mocks.decideSuggestion.mockImplementation(() => new Promise((_, reject) => { fail = reject; }));
		mocks.loadComments.mockResolvedValue(undefined);
		render({ state: { kind: 'list' }, comments: [suggestion, secondSuggestion] });
		const acceptAll = () => host.querySelector<HTMLButtonElement>('.suggestion-status button')!;
		expect(acceptAll().textContent).toBe('Accept all from agent-1');
		acceptAll().click(); acceptAll().click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		flushSync();
		expect(acceptAll().textContent).toBe('Accepting…');
		expect(acceptAll().disabled).toBe(true);
		expect(host.querySelector('[role="alert"]')).toBeNull();
		fail(new Error('The document could not be saved'));
		await vi.waitFor(() => expect(host.querySelector('.suggestion-error[role="alert"]')?.textContent).toBe('The document could not be saved'));
		expect(mocks.decideSuggestion).toHaveBeenCalledExactlyOnceWith('comment-1', 'accept', true);
		expect(mocks.loadComments).not.toHaveBeenCalled();
		expect(acceptAll().textContent).toBe('Accept all from agent-1');
		expect(acceptAll().disabled).toBe(false);
	});

	it('hides accept-all from read-only viewers', () => {
		render({
			state: { kind: 'list' },
			comments: [suggestion, secondSuggestion],
			readOnly: true,
		});
		expect(host.textContent).not.toContain('Accept all from agent-1');
	});
});

describe('DocCommentSurface comments on images inside a document', () => {
	const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
	const text = (id: string, from: number) => ({ id, anchor: id, author: 'you', text: id, at: 'now', resolved: false, replies: [], from, to: from + 2 });
	const onImage = (id: string, number: number, from: number, extra: Record<string, unknown> = {}) => ({
		...text(id, from), anchor: '![Mock](a.png)', to: from + 14, region: BOX, regionNumber: number, imageSrc: '/ws/a.png', ...extra,
	});
	const comments = [
		text('before', 0), onImage('b', 2, 10, { regionStale: true }), text('between', 30), onImage('a', 1, 10),
		onImage('c', 1, 40), onImage('gone', 3, 10, { orphaned: true }),
	];
	const images = () => ({ srcOf: vi.fn((comment: { id: string }) => `/img/${comment.id}`), colorOf: vi.fn(() => '#e5484d'), onOpen: vi.fn() });

	it('lists each image\'s comments together under its thumbnail, in number order, at the image\'s place', () => {
		render({ state: { kind: 'list' }, comments, images: images() });
		const ids = [...host.querySelectorAll('[data-doc-comments-drawer] [data-comment-id]')].map((card) => card.getAttribute('data-comment-id'));
		expect(ids).toEqual(['before', 'a', 'b', 'between', 'c', 'gone']);
		const thumbs = [...host.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
		expect(thumbs).toHaveLength(3);
		expect(thumbs[0].nextElementSibling?.getAttribute('data-comment-id')).toBe('a');
		expect(thumbs[0].querySelector('img')!.getAttribute('src')).toBe('/img/a');
		expect(host.querySelector('[data-comment-id="a"] .ct-num')!.textContent).toBe('1');
	});

	it('names each image beside its thumbnail by its alt text, and in the thumbnail\'s accessible name', () => {
		const named = [onImage('a', 1, 10, { anchor: '![Settings screen](a.png)' }), onImage('c', 1, 40, { anchor: '![](c.png)' })];
		render({ state: { kind: 'list' }, comments: named, images: images() });
		const thumbs = [...host.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
		expect(thumbs[0].querySelector('[data-thumb-caption]')!.textContent).toBe('Settings screen');
		expect(thumbs[0].getAttribute('aria-label')).toBe('Settings screen: image with comment 1');
		// No alt text: no caption, and the name says only what it is.
		expect(thumbs[1].querySelector('[data-thumb-caption]')).toBeNull();
		expect(thumbs[1].getAttribute('aria-label')).toBe('image with comment 1');
	});

	it('opens the image from its thumbnail and places a stale pin again there; an image no longer in the text has nowhere to open', () => {
		const groups = images();
		const onFocus = vi.fn();
		render({ state: { kind: 'list' }, comments, images: groups, onFocus });
		const thumbs = [...host.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
		thumbs[0].click();
		expect(groups.onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }));
		expect(thumbs[2].disabled).toBe(true);
		host.querySelector<HTMLButtonElement>('[data-comment-id="b"] [data-move-pin]')!.click();
		expect(groups.onOpen).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'b' }), { id: 'b', move: true });
		host.querySelector<HTMLButtonElement>('[data-comment-id="a"] .ct-num')!.click();
		expect(onFocus).toHaveBeenCalledWith('a');
	});

	it('keeps a thread being replied to in the Open list when someone else resolves it, until the reply is cleared', async () => {
		const plain = { ...suggestion, replacement: undefined, acked: false };
		const comments = writable([plain, { ...plain, id: 'comment-2', anchor: 'other text' }]);
		const state = fromStore(comments);
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
		render({ state: { kind: 'list' }, get comments() { return state.current; } });
		host.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] .ct-act.reply')!.click();
		await vi.waitFor(() => expect(host.querySelector('[data-comment-id="comment-1"] textarea')).not.toBeNull());
		const field = host.querySelector<HTMLTextAreaElement>('[data-comment-id="comment-1"] textarea')!;
		field.value = 'half a reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		expect(host.querySelector('.drawer-heading')?.textContent).toContain('2 open');
		// A resolved thread also reads as agreed; the kept card must still show the reply being typed, not "Agreed".
		const eased: Array<[Element, number]> = [];
		const animate = vi.spyOn(Element.prototype, 'animate').mockImplementation(function (this: Element, keyframes: Keyframe[] | PropertyIndexedKeyframes | null) {
			eased.push([this, Array.isArray(keyframes) ? keyframes.length : 0]);
			return { cancel() {}, finish() {}, addEventListener() {}, removeEventListener() {}, currentTime: 0, playState: 'finished', finished: Promise.resolve() } as unknown as Animation;
		});
		comments.set([{ ...plain, resolved: true, acked: true }, { ...plain, id: 'comment-2', anchor: 'other text' }]); flushSync();
		const card = host.querySelector<HTMLElement>('[data-comment-id="comment-1"]')!;
		expect(card).not.toBeNull();
		expect(card.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('half a reply');
		// The header counts threads that are open, as the document's chip does: the kept card says it is resolved and is not counted.
		expect(host.querySelector('.drawer-heading')?.textContent).toContain('1 open');
		// It no longer offers what a resolved thread cannot do, says what Send does, and offers Re-open.
		// The note enters, and Agree and Resolve leave, by easing (clip and fade) rather than in one frame.
		const easedNodes = eased.map(([node]) => node);
		expect(easedNodes).toContain(card.querySelector('[data-resolved-note]')!.parentElement);
		expect(easedNodes).toContain(card.querySelector('[aria-label="Resolve"]'));
		expect(easedNodes).toContain(card.querySelector('.ct-act.ack'));
		animate.mockRestore();
		expect(card.querySelector('.ct-agreed')).toBeNull();
		expect(card.querySelector('[data-resolved-note]')?.textContent).toContain('Send adds your reply; the thread stays resolved.');
		expect(card.querySelector('[data-resolved-note] .reopen')?.textContent).toBe('Re-open');
		const cleared = card.querySelector<HTMLTextAreaElement>('textarea')!;
		cleared.value = ''; cleared.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		// It eases out (still mounted, as the card, while it does) and is removed once the ease is done.
		expect(host.querySelector('[data-comment-id="comment-1"]')).not.toBeNull();
		expect(host.querySelector('[data-comment-id="comment-1"] [data-resolved-note]')).not.toBeNull();
		await vi.waitFor(() => expect(host.querySelector('[data-comment-id="comment-1"]')).toBeNull());
		expect(host.querySelector('[data-comment-id="comment-2"]')).not.toBeNull();
		expect(host.querySelector('.drawer-heading')?.textContent).toContain('1 open');
	});

	it('Re-open on the kept card reopens the thread and keeps the reply', async () => {
		const plain = { ...suggestion, replacement: undefined };
		const comments = writable([plain]);
		const state = fromStore(comments);
		const onResolve = vi.fn();
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
		render({ state: { kind: 'list' }, get comments() { return state.current; }, onResolve });
		host.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] .ct-act.reply')!.click();
		await vi.waitFor(() => expect(host.querySelector('[data-comment-id="comment-1"] textarea')).not.toBeNull());
		const field = host.querySelector<HTMLTextAreaElement>('[data-comment-id="comment-1"] textarea')!;
		field.value = 'half a reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		comments.set([{ ...plain, resolved: true }]); flushSync();
		host.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] [data-resolved-note] .reopen')!.click();
		expect(onResolve).toHaveBeenCalledWith('comment-1', false);
	});
});
