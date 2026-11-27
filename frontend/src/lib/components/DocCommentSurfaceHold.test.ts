import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({
	docComments: { subscribe: (run: (value: object) => void) => { run({}); return () => {}; } }, loadComments: vi.fn(),
	pickedComments: { set: () => {}, subscribe: (run: (value: Set<string>) => void) => { run(new Set()); return () => {}; } },
}));

import DocCommentSurface from './DocCommentSurface.svelte';

/**
 * Agree keeps the thread sheet's height, so its new line scrolls inside it and neither the sheet nor the page moves, as when earlier replies are shown.
 */
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const target = { instanceId: 'tab-1', path: 'notes.md', name: 'Notes', projectId: 'project-1', threadId: 'thread-1', artifactId: 'artifact-1' };
const comment = { id: 'comment-1', anchor: 'some text', author: 'agent-1', authorKind: 'agent' as const, text: 'Is this ring confirmed?', at: 'now', resolved: false, replies: [] };
const SHEET_HEIGHT = 410, CAP = 734;

function openThreadSheet() {
	host = document.body.appendChild(document.createElement('div'));
	const onAgree = vi.fn(), onResolve = vi.fn();
	component = mount(DocCommentSurface, {
		target: host,
		props: {
			state: { kind: 'thread', id: comment.id }, anchor: document.createElement('button'), narrow: true, comments: [comment], focusedId: null, draft: null,
			onClose: vi.fn(), restoreFocus: vi.fn(), onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(), onResolve, onAgree, target, artifactId: 'artifact-1',
		},
	});
	flushSync();
	const panel = document.querySelector<HTMLElement>('.drawer-panel')!;
	// jsdom has no layout: the sheet is given the height it has, and the cap the reveal sets from the text above it.
	vi.spyOn(panel, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 400, 390, SHEET_HEIGHT));
	panel.style.setProperty('--drawer-text-cap', `${CAP}px`);
	return { panel, onAgree };
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	document.body.replaceChildren();
	vi.restoreAllMocks();
});

describe('the thread sheet keeps its height when Agree is pressed', () => {
	it('holds the height the sheet has when Agree is pressed, and still agrees', () => {
		const { panel, onAgree } = openThreadSheet();
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe(`${SHEET_HEIGHT}px`);
		expect(onAgree).toHaveBeenCalledWith(comment.id, true);
	});

	it('leaves a sheet that has no cap as it is', () => {
		const { panel, onAgree } = openThreadSheet();
		panel.style.removeProperty('--drawer-text-cap');
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('');
		expect(onAgree).toHaveBeenCalledTimes(1);
	});
});
