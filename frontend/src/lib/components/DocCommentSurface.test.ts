import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';
import type { Acknowledger } from '$lib/api/docCommentTypes';

const mocks = vi.hoisted(() => ({
	decideSuggestion: vi.fn(), flushDraft: vi.fn(), seedDraft: vi.fn(), loadComments: vi.fn(),
}));
vi.mock('$lib/api/commentSuggestions', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/api/commentSuggestions')>(), decideSuggestion: mocks.decideSuggestion,
}));
vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: mocks.flushDraft, seedDraft: mocks.seedDraft }));
vi.mock('$lib/stores/docComments', () => ({
	docComments: { subscribe: (run: (value: object) => void) => { run({}); return () => {}; } }, loadComments: mocks.loadComments,
	arrivedDocComments: { set: () => {}, subscribe: (run: (value: Set<string>) => void) => { run(new Set()); return () => {}; } },
}));

import DocCommentSurface from './DocCommentSurface.svelte';
import { SuggestionRefused } from '$lib/api/commentSuggestions';
import { suggestionRefusals } from '$lib/stores/suggestionRefusals';
import { paneCollapsed } from '$lib/stores/paneCollapsed';
import { sidebarCollapsed } from '$lib/stores/ui';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { createCommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';

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
// A desktop canvas too narrow for the margin column: 600 px wide (under MARGIN_MIN), so a thread opens as a card on its text, as it did
// before the column, whatever the sidebar and the Workspace panel are doing.
let canvases: HTMLElement[] = [];
function noRoomCanvas(): HTMLElement {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	const box = (parent: HTMLElement, className: string, rect: DOMRect) => {
		const el = parent.appendChild(document.createElement('div'));
		el.className = className; el.getBoundingClientRect = () => rect;
		return el;
	};
	const app = box(document.body, 'app-content', new DOMRect(0, 0, 1000, 800));
	box(app, 'chat-col', new DOMRect(0, 0, 400, 800));
	const body = box(app, 'leaf-body', new DOMRect(400, 0, 600, 800));
	const doc = box(body, 'doc', new DOMRect(400, 0, 600, 800));
	canvases.push(app);
	return box(doc, 'bounds', new DOMRect(0, 0, 600, 800));
}
/** The resolved drawer's two bound props, as the viewer holds them for its header control. */
function resolvedDrawerProps() {
	const open = writable(false), liveOpen = fromStore(open);
	let count = 0;
	return {
		props: Object.getOwnPropertyDescriptors({ get resolvedOpen() { return liveOpen.current; }, set resolvedOpen(value: boolean) { open.set(value); }, get resolvedCount() { return count; }, set resolvedCount(value: number) { count = value; } }),
		set: (value: boolean) => open.set(value), isOpen: () => get(open), count: () => count,
	};
}
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	canvases.forEach((canvas) => canvas.remove()); canvases = [];
	vi.unstubAllGlobals();
	suggestionRefusals.set({});
	vi.clearAllMocks();
});

describe('DocCommentSurface suggestions', () => {
	it.each([{ kind: 'list' }, { kind: 'thread', id: suggestion.id }] as const)(
		'exposes the document path on the comment host in $kind view', (state) => {
			render({ state, target: { ...target, path: '/workspace/shared/conductor/x.md' } });
			const body = document.body.querySelector('.md-content');
			expect(body?.closest('[data-doc-path]')?.getAttribute('data-doc-path')).toBe('/workspace/shared/conductor/x.md');
		},
	);

	it('shows open threads as cards in the margin, with text navigation, and lists only resolved ones in the column\'s place while they are open', () => {
		const onFocus = vi.fn();
		const open = resolvedDrawerProps();
		render(Object.defineProperties({ state: { kind: 'list' }, comments: [suggestion, { ...secondSuggestion, resolved: true }], onFocus }, open.props));
		const drawer = document.body.querySelector<HTMLElement>('[data-doc-comments-drawer]');
		expect(drawer).not.toBeNull();
		expect(document.body.querySelector('[data-popover]')).toBeNull();
		expect([...drawer!.querySelectorAll('[data-comment-id]')].map((card) => card.getAttribute('data-comment-id'))).toEqual(['comment-1']);
		drawer!.querySelector<HTMLButtonElement>('[data-comment-jump]')!.click();
		expect(onFocus).toHaveBeenCalledWith(suggestion.id);
		expect(drawer!.querySelector('[data-drawer-filter]')).toBeNull();
		// No heading or close in the column: the header's control opens the resolved drawer.
		expect(drawer!.querySelector('.drawer-heading')).toBeNull();
		expect(document.body.querySelector('[data-resolved-column]')).toBeNull();
		open.set(true); flushSync();
		const list = document.body.querySelector('[data-resolved-column]')!;
		expect([...list.querySelectorAll('[data-comment-id]')].map((card) => card.getAttribute('data-comment-id'))).toEqual(['comment-2']);
		// It takes the column's place, with no sheet or scrim, and the cards are back once it closes.
		expect(document.body.querySelector('[data-adaptive-drawer], [data-popover]')).toBeNull();
		expect(list.querySelector('.drawer-heading')?.textContent).toContain('Resolved 1');
		open.set(false); flushSync();
		expect(document.body.querySelector('[data-resolved-column]')).toBeNull();
		expect(document.body.querySelector('.canvas-comment-drawer.margin [data-comment-id="comment-1"]')).not.toBeNull();
	});

	it('gives the column back to the cards when a comment is picked while the resolved list holds its place', () => {
		const open = resolvedDrawerProps();
		const state = writable<{ kind: 'list' } | { kind: 'thread'; id: string }>({ kind: 'list' }), liveState = fromStore(state);
		const opened = writable(0), liveOpened = fromStore(opened);
		render(Object.defineProperties({ get state() { return liveState.current; }, get opened() { return liveOpened.current; }, comments: [suggestion, { ...secondSuggestion, resolved: true }] }, open.props));
		open.set(true); flushSync();
		expect(document.body.querySelector('[data-resolved-column]')).not.toBeNull();
		state.set({ kind: 'thread', id: suggestion.id }); opened.set(1); flushSync();
		expect(open.isOpen()).toBe(false);
		expect(document.body.querySelector('[data-resolved-column], [data-popover]')).toBeNull();
		expect(document.body.querySelector('.canvas-comment-drawer.margin [data-comment-id="comment-1"]')).not.toBeNull();
	});

	it('gives the column back to the picked card when a highlight is picked while the resolved list holds the place of a column at rest', () => {
		const open = resolvedDrawerProps();
		const surface = createCommentSurfaceState(() => {}, undefined, () => true);
		const focused = fromStore(focusedDocComment);
		surface.state = { kind: 'list' };
		try {
			render(Object.defineProperties({
				get state() { return surface.state; }, get opened() { return surface.opened; }, get focusedId() { return focused.current; },
				comments: [suggestion, { ...secondSuggestion, resolved: true }],
			}, open.props));
			open.set(true); flushSync();
			expect(document.body.querySelector('[data-resolved-column]')).not.toBeNull();
			// A highlight picked in the text: the column at rest stays a list, and its card is the picked one.
			surface.focusComment(suggestion.id, new DOMRect(), false, true); flushSync();
			expect(open.isOpen()).toBe(false);
			expect(document.body.querySelector('[data-resolved-column], [data-popover]')).toBeNull();
			expect(document.body.querySelector('.canvas-comment-drawer.margin [data-comment-id="comment-1"] .ct-input'), 'the picked card shows in the column').not.toBeNull();
		} finally {
			focusedDocComment.set(null);
		}
	});

	it('keeps the resolved list open when what is shown changes without the reader opening anything', () => {
		const open = resolvedDrawerProps();
		const state = writable<{ kind: 'list' } | { kind: 'thread'; id: string; ids?: string[] }>({ kind: 'thread', id: suggestion.id, ids: [suggestion.id, 'other'] }), liveState = fromStore(state);
		render(Object.defineProperties({ get state() { return liveState.current; }, opened: 3, comments: [suggestion, { ...secondSuggestion, resolved: true }] }, open.props));
		open.set(true); flushSync();
		// The Resolved list holds the column's place: the picked card waits for the column, never over the text.
		expect(document.body.querySelector('[data-popover]'), 'no card over the text beside the Resolved list').toBeNull();
		// The shown thread resolved by someone else moves to the next in its stack, then a picked card is let go back to the column.
		state.set({ kind: 'thread', id: 'other', ids: [suggestion.id, 'other'] }); flushSync();
		expect(document.body.querySelector('[data-popover]'), 'no card over the text after the stack moves').toBeNull();
		state.set({ kind: 'list' }); flushSync();
		expect(open.isOpen()).toBe(true);
		expect(document.body.querySelector('[data-resolved-column]')).not.toBeNull();
	});

	it('shows a picked card in the column again once the Resolved list that held its place closes', () => {
		const open = resolvedDrawerProps();
		render(Object.defineProperties({ state: { kind: 'thread', id: suggestion.id }, comments: [suggestion, { ...secondSuggestion, resolved: true }] }, open.props));
		open.set(true); flushSync();
		expect(document.body.querySelector('[data-popover]')).toBeNull();
		open.set(false); flushSync();
		expect(document.body.querySelector('[data-popover]')).toBeNull();
		expect(document.body.querySelector(`.canvas-comment-drawer.margin [data-comment-id="${suggestion.id}"]`)).not.toBeNull();
	});

	it('opens the resolved list and the comment list as popovers from their controls on a desktop canvas with no room for the column', () => {
		const bounds = noRoomCanvas();
		const control = bounds.appendChild(document.createElement('button'));
		control.setAttribute('data-resolved-entry', '');
		const open = resolvedDrawerProps();
		render(Object.defineProperties({ bounds, state: null, comments: [suggestion, { ...secondSuggestion, resolved: true }] }, open.props));
		open.set(true); flushSync();
		const pop = document.body.querySelector('[data-popover]')!;
		expect([...pop.querySelectorAll('[data-comment-id]')].map((card) => card.getAttribute('data-comment-id'))).toEqual(['comment-2']);
		expect(document.body.querySelector('[data-adaptive-drawer], [data-resolved-column]')).toBeNull();
		// A press on its own control is the control's toggle, not an outside press.
		control.dispatchEvent(new Event('pointerdown', { bubbles: true })); flushSync();
		expect(open.isOpen()).toBe(true);
		document.body.dispatchEvent(new Event('pointerdown', { bubbles: true })); flushSync();
		expect(open.isOpen()).toBe(false);
		unmount(component!); component = null; host.remove();
		const pill = document.createElement('button');
		render({ bounds, anchor: pill, state: { kind: 'list' }, comments: [suggestion] });
		expect(document.body.querySelector('[data-popover] [data-doc-comments-drawer] [data-comment-id="comment-1"]')).not.toBeNull();
		expect(document.body.querySelector('[data-popover] .drawer-heading')?.textContent).toContain('Comments 1');
		expect(document.body.querySelector('[data-adaptive-drawer]')).toBeNull();
	});

	it('brings the picked thread into view in the list popover after the popover has moved to the page body, once', async () => {
		// Moving a node resets its scroll, and the popover moves to the page body after its threads mount: a scroll from the
		// thread's own mount is undone by the move, so the one that counts lands after the popover is placed.
		const calls: { id: string | null; inBody: boolean }[] = [];
		const original = Element.prototype.scrollIntoView;
		Element.prototype.scrollIntoView = function (this: Element) {
			calls.push({ id: this.closest('[data-comment-id]')?.getAttribute('data-comment-id') ?? null, inBody: this.closest('[data-popover]')?.parentElement === document.body });
		};
		try {
			render({ bounds: noRoomCanvas(), anchor: document.createElement('button'), state: { kind: 'list' }, comments: [suggestion, secondSuggestion], focusedId: secondSuggestion.id });
			const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			await tick(); await tick(); await frame();
			expect(calls.at(-1)).toEqual({ id: secondSuggestion.id, inBody: true });
			// A later placement (a resize, a scroll elsewhere) leaves the reader's own scroll in the list alone.
			const count = calls.length;
			window.dispatchEvent(new Event('resize')); await tick(); await frame();
			expect(calls.length).toBe(count);
		} finally {
			Element.prototype.scrollIntoView = original;
		}
	});

	it('shows no Reply field and no quote line on a margin card nobody picked, and both on the picked one', () => {
		const rest = { ...secondSuggestion, replacement: null, anchor: 'beta', text: 'A plain comment', replies: [{ id: 'r1', author: 'agent-1', authorKind: 'agent' as const, text: 'One reply', at: 'now' }] };
		render({ state: { kind: 'list' }, comments: [suggestion, rest], focusedId: rest.id });
		const card = (id: string) => document.body.querySelector<HTMLElement>(`[data-doc-comments-drawer] [data-comment-id="${id}"]`)!;
		// Unpicked: author, text, the edit and its Accept/Reject; the jump to the text stays for the keyboard but is out of sight.
		expect(card(suggestion.id).querySelector('.ct-input')).toBeNull();
		expect(card(suggestion.id).querySelector('[data-comment-jump]')?.classList.contains('rest')).toBe(true);
		expect(card(suggestion.id).textContent).toContain('Prefer this wording');
		expect(card(suggestion.id).querySelector('.ct-suggestion-accept')).not.toBeNull();
		// Picked: the field and the quote line, as before.
		expect(card(rest.id).querySelector('.ct-input')).not.toBeNull();
		expect(card(rest.id).querySelector('[data-comment-jump]')?.classList.contains('rest')).toBe(false);
		expect(card(rest.id).textContent).toContain('1 reply');
	});

	it('counts resolved threads for the header control, and opens their drawer only from it', () => {
		const open = resolvedDrawerProps();
		const focused = writable<string | null>(null), liveFocused = fromStore(focused);
		const comments = writable([suggestion, { ...secondSuggestion, resolved: true }]), liveComments = fromStore(comments);
		render(Object.defineProperties({ state: { kind: 'list' }, get focusedId() { return liveFocused.current; }, get comments() { return liveComments.current; } }, open.props));
		expect(open.count()).toBe(1);
		// A resolved thread picked, the comments changing under an edit, or the picked thread being resolved: none opens it over the cards.
		focused.set('comment-2'); flushSync();
		comments.set([{ ...suggestion }, { ...secondSuggestion, resolved: true }]); flushSync();
		focused.set('comment-1'); flushSync();
		comments.set([{ ...suggestion, resolved: true }, { ...secondSuggestion, resolved: true }]); flushSync();
		expect(open.isOpen()).toBe(false);
		expect(document.body.querySelector('.resolved-drawer')).toBeNull();
		open.set(true); flushSync();
		expect(document.body.querySelector('[role="dialog"][aria-label="Resolved comments"], [aria-label="Resolved comments"]')).not.toBeNull();
	});

	it('lists resolved threads newest first', () => {
		const resolved = (id: string, updatedAt: string) => ({ ...suggestion, id, anchor: id, replacement: null, resolved: true, updatedAt });
		const open = resolvedDrawerProps();
		render(Object.defineProperties({ state: { kind: 'list' }, comments: [resolved('old', '2026-11-01T10:00:00Z'), resolved('newest', '2026-11-05T10:00:00Z'), resolved('middle', '2026-11-03T10:00:00Z')] }, open.props));
		open.set(true); flushSync();
		expect([...document.body.querySelectorAll('[data-resolved-column] [data-comment-id]')].map((card) => card.getAttribute('data-comment-id'))).toEqual(['newest', 'middle', 'old']);
	});

	it.each([true, false])('keeps the margin cards up while a new comment is written, the composer a card among them when the column is up (%s)', (overColumn) => {
		render({ state: { kind: 'draft' }, draftOverColumn: overColumn, draft: { text: 'old text' }, comments: [suggestion] });
		expect(document.body.querySelector('.canvas-comment-drawer.margin [data-comment-id="comment-1"]') !== null).toBe(overColumn);
		// With the column up the composer is in it, level with the selection, and nothing covers the text; without it, a popover.
		expect(document.body.querySelector('.canvas-comment-drawer.margin [data-margin-card="draft"] textarea, .canvas-comment-drawer.margin [data-margin-card="draft"] .ct-input') !== null).toBe(overColumn);
		expect(document.body.querySelector('[data-popover] .ct-input, [data-popover] textarea') !== null).toBe(!overColumn);
	});

	it('opens the column without hiding the sidebar or the Workspace panel', () => {
		sidebarCollapsed.set(false); paneCollapsed.set(false);
		render({ state: { kind: 'list' }, comments: [suggestion] });
		expect(document.body.querySelector('.canvas-comment-drawer.margin')).not.toBeNull();
		expect(get(sidebarCollapsed)).toBe(false);
		expect(get(paneCollapsed)).toBe(false);
		unmount(component!); component = null; host.remove();
		expect(get(sidebarCollapsed)).toBe(false);
		expect(get(paneCollapsed)).toBe(false);
	});

	it('keeps a thread whose original text was deleted as a card in the column, labelled and marked like any text change', () => {
		const deleted = { ...suggestion, replacement: null, orphaned: true, detached: true };
		render({ state: { kind: 'list' }, comments: [deleted, secondSuggestion] });
		const card = document.body.querySelector<HTMLElement>('[data-margin-card="comment-1"]');
		expect(card).not.toBeNull();
		expect(card!.querySelector('.ct-orphan')?.textContent).toBe('This text isn’t in the document');
		expect(card!.querySelector('.ct.orphaned')).not.toBeNull();
		expect(document.body.querySelector('[data-margin-card="comment-2"]')).not.toBeNull();
	});

	it('shows a review conversation as a card in the column', () => {
		render({ state: { kind: 'list' }, comments: [suggestion, { ...secondSuggestion, id: 'review-1', replacement: null, review: true }] });
		expect(document.body.querySelector('[data-margin-card="review-1"]')).not.toBeNull();
	});

	it('counts no resolved threads for the header control once nothing is resolved', () => {
		const open = resolvedDrawerProps();
		const comments = writable([suggestion, { ...secondSuggestion, resolved: true }]), liveComments = fromStore(comments);
		render(Object.defineProperties({ state: { kind: 'list' }, get comments() { return liveComments.current; } }, open.props));
		expect(open.count()).toBe(1);
		comments.set([suggestion]); flushSync();
		expect(open.count()).toBe(0);
		expect(document.body.querySelector('[data-margin-card="comment-1"]')).not.toBeNull();
	});

	it('lists only the open threads in the sheet that has no margin; the resolved ones, newest first, open in their own drawer from the header control', async () => {
		const resolved = (id: string, updatedAt: string) => ({ ...suggestion, id, anchor: id, replacement: null, resolved: true, updatedAt });
		const open = resolvedDrawerProps();
		render(Object.defineProperties({ narrow: true, state: { kind: 'list' }, comments: [suggestion, resolved('old', '2026-11-01T10:00:00Z'), resolved('new', '2026-11-05T10:00:00Z')] }, open.props));
		const ids = (selector: string) => [...document.body.querySelectorAll(`${selector} [data-comment-id]`)].map((card) => card.getAttribute('data-comment-id'));
		expect(ids('[data-doc-comments-drawer]')).toEqual(['comment-1']);
		expect(document.body.querySelector('[data-doc-comments-drawer] .drawer-heading')?.textContent).toContain('Comments 1');
		expect(open.count()).toBe(2);
		open.set(true); flushSync(); await tick();
		expect(ids('.resolved-drawer')).toEqual(['new', 'old']);
		expect(document.body.querySelector('[data-doc-comments-drawer] .resolved-drawer')).toBeNull();
	});

	it('opens the resolved drawer with no comment surface up, so a narrow canvas reaches it from the header control alone', async () => {
		const open = resolvedDrawerProps();
		render(Object.defineProperties({ narrow: true, state: null, comments: [suggestion, { ...secondSuggestion, resolved: true }] }, open.props));
		expect(document.body.querySelector('[data-doc-comments-drawer]')).toBeNull();
		expect(open.count()).toBe(1);
		open.set(true); flushSync(); await tick();
		expect([...document.body.querySelectorAll('.resolved-drawer [data-comment-id]')].map((card) => card.getAttribute('data-comment-id'))).toEqual(['comment-2']);
	});

	it('has no empty-resolved text in the sheet while nothing is resolved', () => {
		render({ narrow: true, state: { kind: 'list' }, comments: [suggestion] });
		expect(document.body.querySelector('[data-doc-comments-drawer]')!.textContent).not.toContain('No resolved comments');
	});

	it('has no Send footer: each held lane is sent from its own header or bar', () => {
		render({ state: { kind: 'list' }, comments: [suggestion] });
		expect(document.body.querySelector('[data-doc-comments-drawer] [data-drawer-unsent]')).toBeNull();
		expect(document.body.querySelector('[data-doc-comments-drawer]')!.textContent).not.toContain('Send now');
	});

	it.each([false, true])('keeps a comment whose text moved or went as a marked card (sheet: %s)', (narrow) => {
		render({ narrow, state: { kind: 'list' }, comments: [{ ...suggestion, orphaned: true }, { ...secondSuggestion, orphaned: true, detached: true }] });
		const drawer = document.body.querySelector<HTMLElement>('[data-doc-comments-drawer]')!;
		expect(drawer.querySelectorAll('[data-comment-id] .ct.orphaned')).toHaveLength(2);
	});

	it('keeps the drawer while an image opened from a card covers it, then gives that card focus back', async () => {
		const covering = writable(false);
		const live = fromStore(covering);
		render({ state: { kind: 'list' }, comments: [suggestion, secondSuggestion], get covered() { return live.current; } });
		await tick();
		const card = document.body.querySelector<HTMLElement>('[data-comment-id="comment-2"]')!;
		const control = card.querySelector<HTMLButtonElement>('button:not(:disabled)')!;
		// A click on the card's text focuses nothing; what it opened from is still known.
		card.click();
		covering.set(true); flushSync();
		expect(document.body.querySelector('[data-doc-comments-drawer]')).not.toBeNull();
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

	it.each([false, true])('draws one thread flat in the popover and the sheet: the container is its card (narrow %s)', (narrow) => {
		render({ narrow, state: { kind: 'thread', id: suggestion.id }, ...(narrow ? {} : { bounds: noRoomCanvas() }) });
		const card = document.body.querySelector<HTMLElement>(`${narrow ? '[data-adaptive-drawer]' : '[data-popover]'} .ct`)!;
		expect(card.classList.contains('flat')).toBe(true);
		// Only the sheet shows the thread's replies in full, so only there may its count give way to the name.
		expect(!!card.closest('[data-sheet-thread]')).toBe(narrow);
	});

	it('keeps the reply count context off the list sheet', () => {
		render({ narrow: true, state: { kind: 'list' } });
		expect(document.body.querySelector('[data-adaptive-drawer] .ct')).not.toBeNull();
		expect(document.body.querySelector('[data-sheet-thread]')).toBeNull();
	});

	it('leaves the focus in the text a card was picked from, and moves it into a card opened any other way', async () => {
		const reading = () => new DOMRect(520, 100, 20, 20);
		const text = document.body.appendChild(document.createElement('textarea'));
		try {
			text.focus();
			render({ anchor: reading, bounds: noRoomCanvas(), state: { kind: 'thread', id: suggestion.id } });
			await vi.waitFor(() => expect(document.body.querySelector('[data-popover]')?.contains(document.activeElement)).toBe(true));
			unmount(component!); host.remove(); component = null;
			text.focus();
			render({ anchor: reading, bounds: noRoomCanvas(), state: { kind: 'thread', id: suggestion.id, fromText: true } });
			await vi.waitFor(() => expect(document.body.querySelector('[data-popover]')?.hasAttribute('aria-hidden')).toBe(false));
			await new Promise((resolve) => setTimeout(resolve, 50));
			expect(document.activeElement).toBe(text);
		} finally { text.remove(); }
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
		const popover = () => document.body.querySelector<HTMLElement>('[data-popover]')!;
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
			render({ anchor: reading, bounds: noRoomCanvas(), onClose, get comments() { return live.current; }, ...extra });
			return { set, onClose };
		}

		it('keeps a refused Accept open at its place with the reason showing', async () => {
			mocks.flushDraft.mockResolvedValue(true);
			mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused('The text changed on the server', 409, 'stale'));
			const refused = fromStore(suggestionRefusals);
			drawn = new DOMRect(520, 100, 20, 20);
			const onClose = vi.fn();
			render({ anchor: reading, bounds: noRoomCanvas(), onClose, get comments() { return [{ ...suggestion, orphaned: 'comment-1' in (refused.current['artifact-1'] ?? {}) }]; } });
			await vi.waitFor(() => expect(popover().style.left).toBe('548px'));
			const before = placed();
			// The editor stops drawing the text once the suggestion is refused as stale.
			const accept = document.body.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!;
			drawn = null;
			accept.click();
			await vi.waitFor(() => expect(document.body.querySelector('.ct-orphan')?.textContent).toBe("The text this suggestion replaces isn't in the document."));
			expect(document.body.querySelector('[role="alert"]')?.textContent).toBe('The text changed on the server');
			await vi.waitFor(() => expect(placed()).toEqual(before));
			expect(onClose).not.toHaveBeenCalled();
		});

		it('keeps the card, its place and a reply being typed when a collaborator orphans the comment', async () => {
			const { set, onClose } = renderFollowing({ ...suggestion, replacement: null });
			await vi.waitFor(() => expect(popover().style.left).toBe('548px'));
			const before = placed();
			const input = document.body.querySelector<HTMLTextAreaElement>('.ct-input')!;
			input.value = 'A half-written reply';
			input.dispatchEvent(new Event('input', { bubbles: true }));
			flushSync();
			drawn = null;
			set({ ...suggestion, replacement: null, orphaned: true });
			await vi.waitFor(() => expect(document.body.querySelector('.ct-orphan')).not.toBeNull());
			expect(document.body.querySelector<HTMLInputElement>('.ct-input')?.value).toBe('A half-written reply');
			expect(placed()).toEqual(before);
			expect(onClose).not.toHaveBeenCalled();
		});

		it('keeps the card at its place when a collaborator resolves the comment', async () => {
			const { set, onClose } = renderFollowing({ ...suggestion, replacement: null });
			await vi.waitFor(() => expect(popover().style.left).toBe('548px'));
			const before = placed();
			drawn = null;
			set({ ...suggestion, replacement: null, resolved: true });
			await vi.waitFor(() => expect(document.body.querySelector('summary')?.textContent).toContain('Resolved'));
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
		expect(document.body.textContent).toContain("Couldn't load comments. Check the connection and retry.");
		expect(document.body.textContent).not.toContain('Failed to fetch');
		expect(warn).toHaveBeenCalledWith('Could not load comments', 'Failed to fetch');
		warn.mockRestore();
		expect(document.body.textContent).not.toContain('No comments yet.');
		[...document.body.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry')!.click();
		expect(onRetry).toHaveBeenCalledOnce();
	});
	it('opens Reply for the selected card in a narrow canvas drawer', async () => {
		const original = HTMLElement.prototype.scrollIntoView;
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
		try {
			render({ state: { kind: 'thread', id: suggestion.id }, narrow: true, focusedId: suggestion.id, comments: [{ ...suggestion, replacement: null }] });
			await vi.waitFor(() => expect(document.querySelector<HTMLInputElement>('.ct-input')).not.toBeNull());
			expect(document.activeElement?.classList.contains('ct-input')).not.toBe(true);
		} finally {
			Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: original });
		}
	});
	it('focuses Reply without turning input clicks into text navigation', async () => {
		const onFocus = vi.fn();
		render({ state: { kind: 'list' }, focusedId: suggestion.id, comments: [{ ...suggestion, replacement: null }], onFocus });
		document.body.querySelector<HTMLTextAreaElement>('.ct-input')!.focus();
		await vi.waitFor(() => expect(document.activeElement?.classList.contains('ct-input')).toBe(true));
		document.body.querySelector<HTMLTextAreaElement>('.ct-input')!.click();
		expect(onFocus).not.toHaveBeenCalled();
	});
	it('consumes Escape in the margin column once: focus goes back, the column stays, and the document never sees it', () => {
		const onClose = vi.fn(), restoreFocus = vi.fn(), documentEscape = vi.fn();
		render({ state: { kind: 'list' }, onClose, restoreFocus });
		window.addEventListener('keydown', documentEscape);
		try {
			document.body.querySelector('.canvas-comment-drawer.margin')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
			expect(onClose).not.toHaveBeenCalled();
			expect(restoreFocus).toHaveBeenCalledOnce();
			expect(documentEscape).not.toHaveBeenCalled();
		} finally { window.removeEventListener('keydown', documentEscape); }
	});

	it('closes the comments sheet on Escape without reaching the document', () => {
		const onClose = vi.fn(), documentEscape = vi.fn();
		render({ state: { kind: 'list' }, narrow: true, onClose });
		window.addEventListener('keydown', documentEscape);
		try {
			document.body.querySelector('[data-doc-comments-drawer]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
			// The sheet is modal: it hands focus back itself once it has closed.
			expect(onClose).toHaveBeenCalledOnce();
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
		document.body.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
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
		document.body.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('The text changed on the server'));
		expect(document.body.querySelector('.ct-suggestion-actions')).not.toBeNull();
		expect(onSuggestionAccepted).not.toHaveBeenCalled();
	});

	it('records a refused accept for its card and keeps the reason on the popover card that moves groups, without reloading', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused('The text changed on the server', 409, 'stale'));
		// As DocViewer lists them: a refused card shows as orphaned (commentsInDocumentOrder).
		const refused = fromStore(suggestionRefusals);
		const { onSuggestionAccepted } = render({ bounds: noRoomCanvas(), get comments() { return [{ ...suggestion, orphaned: 'comment-1' in (refused.current['artifact-1'] ?? {}) }]; } });
		const card = () => document.body.querySelector<HTMLElement>('.ct')!;
		const accept = () => document.body.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!;
		// The canvas is measured after the first paint; the card is the one that stays once it has been.
		await tick();
		const before = card();
		// A thread card draws no section heading (the card says it itself), so the group is read off its own note.
		expect(document.body.querySelector('.ct-orphan')).toBeNull();
		expect(accept().disabled).toBe(false);
		accept().click();
		await vi.waitFor(() => expect(document.body.querySelector('.ct-orphan')).not.toBeNull());
		expect(document.body.querySelector('[role="alert"]')?.textContent).toBe('The text changed on the server');
		expect(get(suggestionRefusals)).toEqual({ 'artifact-1': { 'comment-1': 'The text changed on the server' } });
		// The card's note changed, but the card did not move: anything typed in it stays where it was.
		expect(card()).toBe(before);
		// The refusal is the card's state; nothing has to come back from the server to show it.
		expect(mocks.loadComments).not.toHaveBeenCalled();
		expect(accept().textContent).toBe('Accept');
		expect(accept().disabled).toBe(true);
		expect(document.body.querySelector('.ct-orphan')?.textContent).toBe("The text this suggestion replaces isn't in the document.");
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
		document.body.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
		await vi.waitFor(() => expect(document.body.querySelector('[role="alert"]')?.textContent).toBe(message));
		expect(mocks.loadComments).toHaveBeenCalledTimes(loads);
		expect(onDocumentChanged).toHaveBeenCalledTimes(reads);
		expect(get(suggestionRefusals)).toEqual({});
	});

	it('clears a refusal when the next decision on that suggestion succeeds', async () => {
		mocks.decideSuggestion.mockRejectedValueOnce(new Error('Only the owner can decide'));
		mocks.loadComments.mockResolvedValue(undefined);
		const { onSuggestionBusy } = render();
		const reject = () => document.body.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!;
		reject().click();
		await vi.waitFor(() => expect(document.body.querySelector('[role="alert"]')).not.toBeNull());
		mocks.decideSuggestion.mockResolvedValue({ comment: suggestion });
		reject().click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalledTimes(2));
		await vi.waitFor(() => expect(reject().disabled).toBe(false));
		expect(document.body.querySelector('[role="alert"]')).toBeNull();
		expect(onSuggestionBusy).toHaveBeenCalledWith(true, undefined); // a Reject names no suggestion to mark busy
	});

	it('drops a refusal when the document changes', async () => {
		mocks.decideSuggestion.mockRejectedValue(new Error('Only the owner can decide'));
		const artifact = writable('artifact-1');
		const live = fromStore(artifact);
		render({ get artifactId() { return live.current; } });
		document.body.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(document.body.querySelector('[role="alert"]')).not.toBeNull());
		artifact.set('artifact-2'); flushSync();
		expect(document.body.querySelector('[role="alert"]')).toBeNull();
	});

	it('ends a refused accept when the surface closes or the document changes', async () => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused('The text changed on the server', 409, 'stale'));
		const state = writable<{ kind: 'thread'; id: string } | null>({ kind: 'thread', id: suggestion.id });
		const artifact = writable('artifact-1');
		const liveState = fromStore(state), liveArtifact = fromStore(artifact);
		render({ get state() { return liveState.current; }, get artifactId() { return liveArtifact.current; } });
		const refuse = async () => {
			document.body.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!.click();
			await vi.waitFor(() => expect(get(suggestionRefusals)['artifact-1']).toEqual({ 'comment-1': 'The text changed on the server' }));
		};
		await refuse();
		state.set(null); flushSync();
		expect(get(suggestionRefusals)).toEqual({});
		state.set({ kind: 'thread', id: suggestion.id }); flushSync();
		expect(document.body.querySelector('[role="alert"]')).toBeNull();
		await refuse();
		artifact.set('artifact-2'); flushSync();
		expect(get(suggestionRefusals)).toEqual({});
		expect(document.body.querySelector('[role="alert"]')).toBeNull();
	});

	it('does not reload comments after a failed reject, and keeps the refusal on a card that never orphaned', async () => {
		mocks.decideSuggestion.mockRejectedValue(new Error('Network down'));
		const comments = writable([suggestion]);
		const live = fromStore(comments);
		render({ get comments() { return live.current; } });
		document.body.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(document.body.querySelector('[role="alert"]')?.textContent).toBe('Network down'));
		// Reject never saves the draft first, so a reload here would read positions for text the screen no longer has.
		expect(mocks.flushDraft).not.toHaveBeenCalled();
		expect(mocks.loadComments).not.toHaveBeenCalled();
		// Comments refreshed from elsewhere: the card was never orphaned, so its refusal stays.
		comments.set([{ ...suggestion }]); flushSync();
		expect(document.body.querySelector('[role="alert"]')?.textContent).toBe('Network down');
	});

	it('drops a refusal once the surface closes', async () => {
		mocks.decideSuggestion.mockRejectedValue(new Error('Only the owner can decide'));
		const state = writable<{ kind: 'thread'; id: string } | null>({ kind: 'thread', id: suggestion.id });
		const live = fromStore(state);
		render({ get state() { return live.current; } });
		document.body.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(document.body.querySelector('[role="alert"]')).not.toBeNull());
		state.set(null); flushSync();
		state.set({ kind: 'thread', id: suggestion.id }); flushSync();
		expect(document.body.querySelector('.ct-suggestion-actions')).not.toBeNull();
		expect(document.body.querySelector('[role="alert"]')).toBeNull();
	});

	it('shows accept-all only when an author has at least two open suggestions', () => {
		render({ state: { kind: 'list' }, comments: [suggestion] });
		expect(document.body.textContent).not.toContain('Accept all from agent-1');
	});

	it.each([true, false])('accepts all, re-seeds without a stale banner, and shows skipped reasons (sheet: %s)', async (narrow) => {
		mocks.flushDraft.mockResolvedValue(true);
		mocks.decideSuggestion.mockResolvedValue({
			accepted: ['comment-1'],
			skipped: [{ id: 'comment-2', reason: 'overlaps an earlier suggestion' }],
			text: 'accepted document',
			revision: 'r3',
		});
		mocks.loadComments.mockResolvedValue(undefined);
		// Accept all is the sheet list's first item, and the margin column's.
		const { onSuggestionAccepted } = render({
			narrow,
			state: { kind: 'list' },
			comments: [suggestion, secondSuggestion],
		});
		const button = Array.from(document.body.querySelectorAll(narrow ? 'button' : '[data-margin-head] button')).find(
			(candidate) => candidate.textContent === 'Accept all from agent-1',
		) as HTMLButtonElement;
		button.click();
		await vi.waitFor(() => expect(mocks.loadComments).toHaveBeenCalledWith('artifact-1'));
		expect(mocks.decideSuggestion).toHaveBeenCalledWith('comment-1', 'accept', true);
		expect(mocks.seedDraft).toHaveBeenCalledWith(target, 'accepted document', 'r3');
		expect(onSuggestionAccepted).toHaveBeenCalledWith('accepted document');
		expect(document.body.textContent).toContain('1 skipped');
		expect(document.body.textContent).toContain('overlaps an earlier suggestion');
		expect(document.body.textContent).not.toContain('Updated elsewhere');
	});

	it.each([true, false])('runs accept-all once, shows it busy, and reports a failure inline (sheet: %s)', async (narrow) => {
		mocks.flushDraft.mockResolvedValue(true);
		let fail!: (error: Error) => void;
		mocks.decideSuggestion.mockImplementation(() => new Promise((_, reject) => { fail = reject; }));
		mocks.loadComments.mockResolvedValue(undefined);
		render({ narrow, state: { kind: 'list' }, comments: [suggestion, secondSuggestion] });
		const acceptAll = () => document.body.querySelector<HTMLButtonElement>(`${narrow ? '' : '[data-margin-head] '}.suggestion-status button`)!;
		expect(acceptAll().textContent).toBe('Accept all from agent-1');
		acceptAll().click(); acceptAll().click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		flushSync();
		expect(acceptAll().textContent).toBe('Accepting…');
		expect(acceptAll().disabled).toBe(true);
		expect(document.body.querySelector('[role="alert"]')).toBeNull();
		fail(new Error('The document could not be saved'));
		await vi.waitFor(() => expect(document.body.querySelector('.suggestion-error[role="alert"]')?.textContent).toBe('The document could not be saved'));
		expect(mocks.decideSuggestion).toHaveBeenCalledExactlyOnceWith('comment-1', 'accept', true);
		expect(mocks.loadComments).not.toHaveBeenCalled();
		expect(acceptAll().textContent).toBe('Accept all from agent-1');
		expect(acceptAll().disabled).toBe(false);
	});

	it.each([true, false])('hides accept-all from read-only viewers (sheet: %s)', (narrow) => {
		render({
			narrow,
			state: { kind: 'list' },
			comments: [suggestion, secondSuggestion],
			readOnly: true,
		});
		expect(document.body.textContent).not.toContain('Accept all from agent-1');
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

	// The margin column keeps the thumbnail: one heads each image card, in document order. Grouping every comment of an image together under one thumbnail, in number order, and the orphaned card, are the sheet's list (the next two tests).
	it('in the margin column, a thumbnail and caption sit inside the top of each image card, in document order', () => {
		render({ state: { kind: 'list' }, comments, images: images() });
		expect(document.body.querySelector('.canvas-comment-drawer.margin')).not.toBeNull();
		const ids = [...document.body.querySelectorAll('[data-doc-comments-drawer] [data-comment-id]')].map((card) => card.getAttribute('data-comment-id'));
		expect(ids).toEqual(['before', 'b', 'between', 'a', 'c', 'gone']);
		const thumbs = [...document.body.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
		expect(thumbs.map((thumb) => thumb.closest('[data-comment-id]')?.getAttribute('data-comment-id'))).toEqual(['b', 'a', 'c', 'gone']);
		for (const thumb of thumbs) expect(thumb.closest('.ct')?.firstElementChild).toBe(thumb);
		expect(thumbs.map((thumb) => thumb.getAttribute('aria-label'))).toEqual(['Mock: image with comment 2', 'Mock: image with comment 1', 'Mock: image with comment 1', 'Mock: image with comment 3']);
	});

	it('in the margin column, a card\'s thumbnail opens its image', () => {
		const groups = images();
		render({ state: { kind: 'list' }, comments, images: groups });
		const thumbs = [...document.body.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
		thumbs[0].click();
		expect(groups.onOpen).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'b' }));
		thumbs[1].click();
		expect(groups.onOpen).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'a' }));
	});

	it('lists each image\'s comments together under its thumbnail, in number order, at the image\'s place', () => {
		// The sheet's list groups every comment of an image under one thumbnail, in number order, and lists the orphaned card; the margin column's own thumbnails are the two tests above.
		render({ narrow: true, state: { kind: 'list' }, comments, images: images() });
		const ids = [...document.body.querySelectorAll('[data-doc-comments-drawer] [data-comment-id]')].map((card) => card.getAttribute('data-comment-id'));
		expect(ids).toEqual(['before', 'a', 'b', 'between', 'c', 'gone']);
		const thumbs = [...document.body.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
		expect(thumbs).toHaveLength(3);
		expect(thumbs[0].nextElementSibling?.getAttribute('data-comment-id')).toBe('a');
		expect(thumbs[0].querySelector('img')!.getAttribute('src')).toBe('/img/a');
		expect(document.body.querySelector('[data-comment-id="a"] .ct-num')!.textContent).toBe('1');
	});

	it('names a deleted diagram in the stale comment card', () => {
		const pin = { ...text('pin', 10), region: BOX, regionNumber: 1, regionStale: true, orphaned: true };
		render({ state: { kind: 'list' }, comments: [pin], images: { srcOf: () => null, diagramOf: () => null, isDiagram: () => true, colorOf: () => '#e5484d', onOpen: vi.fn() } });
		expect(document.body.querySelector('[data-region-stale]')?.textContent).toContain('Diagram changed since this comment');
	});

	it('names each image beside its thumbnail by its alt text, and in the thumbnail\'s accessible name', () => {
		const named = [onImage('a', 1, 10, { anchor: '![Settings screen](a.png)' }), onImage('c', 1, 40, { anchor: '![](c.png)' })];
		render({ state: { kind: 'list' }, comments: named, images: images() });
		const thumbs = [...document.body.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
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
		const thumbs = [...document.body.querySelectorAll<HTMLButtonElement>('[data-region-thumb]')];
		thumbs[1].click();
		expect(groups.onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }));
		expect(thumbs[3].disabled).toBe(true);
		document.body.querySelector<HTMLButtonElement>('[data-comment-id="b"] [data-move-pin]')!.click();
		expect(groups.onOpen).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'b' }), { id: 'b', move: true });
		document.body.querySelector<HTMLButtonElement>('[data-comment-id="a"] .ct-num')!.click();
		expect(onFocus).toHaveBeenCalledWith('a');
	});

	it('keeps a thread being replied to in the Open list when someone else resolves it, until the reply is cleared', async () => {
		const plain = { ...suggestion, replacement: undefined, acked: false, acknowledgedBy: [] as Acknowledger[] };
		const comments = writable([plain, { ...plain, id: 'comment-2', anchor: 'other text' }]);
		const state = fromStore(comments);
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
		const open = resolvedDrawerProps();
		render(Object.defineProperties({ state: { kind: 'list' }, focusedId: 'comment-1', get comments() { return state.current; } }, open.props));
		const field = document.body.querySelector<HTMLTextAreaElement>('[data-comment-id="comment-1"] textarea')!;
		field.value = 'half a reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		expect(open.count()).toBe(0);
		// A resolved thread also reads as agreed; the kept card must still show the reply being typed, not "Agreed".
		const eased: Array<[Element, number]> = [];
		const animate = vi.spyOn(Element.prototype, 'animate').mockImplementation(function (this: Element, keyframes: Keyframe[] | PropertyIndexedKeyframes | null) {
			eased.push([this, Array.isArray(keyframes) ? keyframes.length : 0]);
			return { cancel() {}, finish() {}, addEventListener() {}, removeEventListener() {}, currentTime: 0, playState: 'finished', finished: Promise.resolve() } as unknown as Animation;
		});
		comments.set([{ ...plain, resolved: true, acked: true, acknowledgedBy: [{ id: 'ui', name: 'Steve', kind: 'user' }] }, { ...plain, id: 'comment-2', anchor: 'other text' }]); flushSync();
		const card = document.body.querySelector<HTMLElement>('[data-comment-id="comment-1"]')!;
		expect(card).not.toBeNull();
		expect(card.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('half a reply');
		// The kept card says it is resolved, and the header's Resolved control does not count it yet.
		expect(open.count()).toBe(0);
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
		expect(document.body.querySelector('[data-comment-id="comment-1"]')).not.toBeNull();
		expect(document.body.querySelector('[data-comment-id="comment-1"] [data-resolved-note]')).not.toBeNull();
		// The picked card is the one the person typed in, so it goes on to the Resolved list the column keeps for the selected thread; what leaves is its place among the open cards.
		await vi.waitFor(() => expect(document.body.querySelector('.margin-stack [data-comment-id="comment-1"]')).toBeNull());
		expect(document.body.querySelector('.margin-stack [data-comment-id="comment-2"]')).not.toBeNull();
		expect(open.count()).toBe(1);
	});

	it('Re-open on the kept card reopens the thread and keeps the reply', async () => {
		const plain = { ...suggestion, replacement: undefined };
		const comments = writable([plain]);
		const state = fromStore(comments);
		const onResolve = vi.fn();
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
		render({ state: { kind: 'list' }, focusedId: 'comment-1', get comments() { return state.current; }, onResolve });
		const field = document.body.querySelector<HTMLTextAreaElement>('[data-comment-id="comment-1"] textarea')!;
		field.value = 'half a reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		comments.set([{ ...plain, resolved: true }]); flushSync();
		document.body.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] [data-resolved-note] .reopen')!.click();
		expect(onResolve).toHaveBeenCalledWith('comment-1', false);
	});
	it('closes the card when a decided suggestion that kept its replies is resolved, once the request is answered, and not for any other comment', async () => {
		const decided = { ...suggestion, decision: 'accepted' as const, replies: [{ id: 'r1', author: 'agent-1', text: 'Thanks', at: 'now' }] };
		let answer!: (taken: boolean) => void;
		const onResolve = vi.fn(() => new Promise<boolean>((resolve) => { answer = resolve; })), onClose = vi.fn(), restoreFocus = vi.fn();
		render({ bounds: noRoomCanvas(), comments: [decided], onResolve, onClose, restoreFocus });
		document.body.querySelector<HTMLButtonElement>('.ct-act.resolve')!.click();
		expect(onResolve).toHaveBeenCalledWith('comment-1', true, true);
		await Promise.resolve();
		expect(onClose).not.toHaveBeenCalled();
		answer(true);
		await vi.waitFor(() => expect(onClose).toHaveBeenCalledOnce());
		expect(restoreFocus).toHaveBeenCalledOnce();
		unmount(component!); component = null; host.remove();
		const plain = { ...suggestion, replacement: undefined };
		const onCloseAgain = vi.fn(), plainResolve = vi.fn();
		render({ comments: [plain], onResolve: plainResolve, onClose: onCloseAgain });
		document.body.querySelector<HTMLButtonElement>('.ct-act.resolve')!.click();
		expect(plainResolve).toHaveBeenCalledWith('comment-1', true);
		await Promise.resolve();
		expect(onCloseAgain).not.toHaveBeenCalled();
	});
	it('keeps the margin column up when a decided suggestion picked from its text is resolved', async () => {
		const decided = { ...suggestion, decision: 'accepted' as const, replies: [{ id: 'r1', author: 'agent-1', text: 'Thanks', at: 'now' }] };
		const other = { ...suggestion, id: 'comment-2', replacement: undefined };
		const onResolve = vi.fn(async () => true), onClose = vi.fn(), restoreFocus = vi.fn();
		render({ state: { kind: 'thread', id: decided.id }, comments: [decided, other], onResolve, onClose, restoreFocus });
		expect(document.body.querySelector('.canvas-comment-drawer.margin')).not.toBeNull();
		document.body.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] .ct-act.resolve')!.click();
		await vi.waitFor(() => expect(onResolve).toHaveBeenCalledWith('comment-1', true, true));
		await Promise.resolve(); await Promise.resolve();
		expect(onClose).not.toHaveBeenCalled();
		expect(document.body.querySelector('[data-comment-id="comment-2"]')).not.toBeNull();
	});

	it.each([false, true])('keeps a decided card and its focus when the request to resolve it is refused (phone width: %s)', async (narrow) => {
		const decided = { ...suggestion, decision: 'accepted' as const, replies: [{ id: 'r1', author: 'agent-1', text: 'Thanks', at: 'now' }] };
		const onResolve = vi.fn(async () => false), onClose = vi.fn(), restoreFocus = vi.fn();
		render({ comments: [decided], onResolve, onClose, restoreFocus, narrow });
		const resolve = document.body.querySelector<HTMLButtonElement>('.ct-act.resolve')!;
		resolve.focus(); resolve.click();
		await vi.waitFor(() => expect(onResolve).toHaveBeenCalledOnce());
		await Promise.resolve(); await Promise.resolve();
		expect(onClose).not.toHaveBeenCalled();
		expect(restoreFocus).not.toHaveBeenCalled();
		expect(document.body.querySelector('.ct-act.resolve')).toBe(resolve);
		await vi.waitFor(() => expect(document.body.textContent).toContain('Couldn’t resolve this. Try again.'));
		// The sheet holds focus for its own card; a refusal must not hand it to the page.
		expect(document.activeElement).not.toBe(document.body);
		if (!narrow) expect(document.activeElement).toBe(resolve);
	});

	it('keeps a decided card in the column when its Resolve is refused there, and says so on the card', async () => {
		const decided = { ...suggestion, decision: 'accepted' as const, replies: [{ id: 'r1', author: 'agent-1', text: 'Thanks', at: 'now' }] };
		const onResolve = vi.fn(async () => false), onClose = vi.fn(), restoreFocus = vi.fn();
		// The column is up with nothing picked: the surface is in its list state, which is where a Resolve pressed on a resting card comes from.
		render({ state: { kind: 'list' }, comments: [decided], onResolve, onClose, restoreFocus });
		const resolve = document.body.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] .ct-act.resolve')!;
		resolve.click();
		await vi.waitFor(() => expect(onResolve).toHaveBeenCalledWith('comment-1', true, true));
		await vi.waitFor(() => expect(document.body.textContent).toContain('Couldn’t resolve this. Try again.'));
		expect(onClose).not.toHaveBeenCalled();
		expect(document.body.querySelector('[data-comment-id="comment-1"] .ct-act.resolve')).toBe(resolve);
	});

	it('reports the margin card mode exactly while its margin column is shown, as the canvas is resized', async () => {
		const resized: (() => void)[] = [];
		vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { resized.push(callback); } observe() {} unobserve() {} disconnect() {} });
		let width = 1000;
		const bounds = document.body.appendChild(document.createElement('div'));
		bounds.getBoundingClientRect = () => new DOMRect(0, 0, width, 800);
		canvases.push(bounds);
		render({ bounds, state: { kind: 'list' } });
		const surface = component as unknown as { cardMode(): string };
		const column = () => document.body.querySelector('.drawer-comments.in-margin') !== null;
		const resize = (to: number) => { width = to; resized.forEach((callback) => callback()); flushSync(); };
		expect([surface.cardMode(), column()]).toEqual(['margin', true]);
		resize(600); // under MARGIN_MIN: the column leaves (after its exit motion) as the mode stops being 'margin'
		expect(surface.cardMode()).toBe('popover');
		await vi.waitFor(() => expect(column()).toBe(false));
		resize(660); // exactly MARGIN_MIN
		expect([surface.cardMode(), column()]).toEqual(['margin', true]);
	});

	it('opens a thread as a popover at a narrow width that is not a phone, and as a sheet on a phone', () => {
		render({ narrow: true, phone: false, bounds: noRoomCanvas(), state: { kind: 'thread', id: suggestion.id } });
		expect(document.body.querySelector('[data-popover]')).not.toBeNull();
		expect(document.body.querySelector('[data-adaptive-drawer]')).toBeNull();
		unmount(component!); component = null; host.remove();
		render({ narrow: true, phone: true, bounds: noRoomCanvas(), state: { kind: 'thread', id: suggestion.id } });
		expect(document.body.querySelector('[data-adaptive-drawer]')).not.toBeNull();
		expect(document.body.querySelector('[data-popover]')).toBeNull();
	});

	it('does not offer accept-all for suggestions that were already decided', () => {
		const decided = (id: string) => ({ ...suggestion, id, decision: 'accepted' as const, replies: [{ id: 'r', author: 'you', text: 'Thanks', at: 'now' }] });
		render({ state: { kind: 'list' }, comments: [decided('a'), decided('b')] });
		expect(document.body.textContent).not.toMatch(/accept all/i);
	});
});
