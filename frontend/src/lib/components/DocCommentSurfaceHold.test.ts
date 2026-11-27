import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({
	docComments: { subscribe: (run: (value: object) => void) => { run({}); return () => {}; } }, loadComments: vi.fn(),
	pickedComments: { set: () => {}, subscribe: (run: (value: Set<string>) => void) => { run(new Set()); return () => {}; } },
}));

import DocCommentSurface from './DocCommentSurface.svelte';
import * as keepInView from '$lib/actions/keepInView';

/**
 * Agree keeps the thread sheet's height, so its new line scrolls inside it and neither the sheet nor the page moves, as when earlier replies are shown.
 */
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const target = { instanceId: 'tab-1', path: 'notes.md', name: 'Notes', projectId: 'project-1', threadId: 'thread-1', artifactId: 'artifact-1' };
const comment = { id: 'comment-1', anchor: 'some text', author: 'agent-1', authorKind: 'agent' as const, text: 'Is this ring confirmed?', at: 'now', resolved: false, replies: [], replyGoesTo: [{ id: 'agent-1', name: 'Reviewer' }] };
const SHEET_HEIGHT = 410, CAP = 734;

function openThreadSheet(onAgree = vi.fn(), image = false) {
	host = document.body.appendChild(document.createElement('div'));
	const onResolve = vi.fn();
	component = mount(DocCommentSurface, {
		target: host,
		props: {
			state: { kind: 'thread', id: comment.id }, anchor: document.createElement('button'), narrow: true, comments: [comment], focusedId: null, draft: null,
			onClose: vi.fn(), restoreFocus: vi.fn(), onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(), onResolve, onAgree, target, artifactId: 'artifact-1',
			image: image ? { src: 'image.png', colorOf: () => 'red' } : null,
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
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('the thread sheet keeps its height when Agree is pressed', () => {
	it('holds the height the sheet has when Agree is pressed, and still agrees', () => {
		const { panel, onAgree } = openThreadSheet();
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe(`${SHEET_HEIGHT}px`);
		expect(onAgree).toHaveBeenCalledWith(comment.id, true);
	});

	it('holds an image thread and preserves its reply without a text cap', () => {
		const arm = vi.spyOn(keepInView, 'preserveVisibleWhileGrowing').mockReturnValue(() => {});
		const { panel, onAgree } = openThreadSheet(vi.fn(), true);
		panel.style.removeProperty('--drawer-text-cap');
		expect(panel.hasAttribute('data-keeps-height')).toBe(true);
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe(`${SHEET_HEIGHT}px`);
		expect(arm).toHaveBeenCalledOnce();
		expect(onAgree).toHaveBeenCalledWith(comment.id, true);
	});

	it('keeps preservation armed for a delayed acknowledgement and cancels it on failure', async () => {
		let answer!: (accepted: boolean) => void;
		const pending = new Promise<boolean>((resolve) => { answer = resolve; });
		const cancel = vi.fn();
		const arm = vi.spyOn(keepInView, 'preserveVisibleWhileGrowing').mockReturnValue(cancel);
		openThreadSheet(vi.fn(() => pending));
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		expect(arm).toHaveBeenCalledOnce();
		expect(cancel).not.toHaveBeenCalled();
		answer(false);
		await pending;
		await Promise.resolve();
		expect(cancel).toHaveBeenCalledOnce();
	});

	it('ends preservation after a successful acknowledgement when the intro never fires', async () => {
		vi.useFakeTimers();
		let armed = false;
		vi.spyOn(keepInView, 'preserveVisibleWhileGrowing').mockImplementation(() => {
			armed = true;
			return () => { armed = false; };
		});
		openThreadSheet(vi.fn(() => Promise.resolve(true)));
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		await Promise.resolve();
		expect(armed).toBe(true);
		await vi.advanceTimersByTimeAsync(179);
		expect(armed).toBe(true);
		await vi.advanceTimersByTimeAsync(21);
		expect(armed).toBe(false);
		vi.useRealTimers();
	});

	it('keeps preservation armed through the reduced-motion intro and one frame', async () => {
		vi.useFakeTimers();
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('prefers-reduced-motion'), addEventListener: () => {}, removeEventListener: () => {} }));
		const finish = vi.fn();
		vi.spyOn(keepInView, 'preserveVisibleWhileGrowing').mockReturnValue(finish);
		openThreadSheet(vi.fn(() => Promise.resolve(true)));
		document.querySelector<HTMLElement>('[data-thread-id]')!.style.setProperty('--dur-tight', '160ms');
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		await Promise.resolve();
		await vi.advanceTimersByTimeAsync(159);
		expect(finish).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(20);
		expect(finish).toHaveBeenCalledWith(true);
	});

	it('leaves a sheet that has no cap as it is', () => {
		const { panel, onAgree } = openThreadSheet();
		panel.style.removeProperty('--drawer-text-cap');
		document.querySelector<HTMLButtonElement>('button[title="Agree"]')!.click();
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('');
		expect(onAgree).toHaveBeenCalledTimes(1);
	});
});
