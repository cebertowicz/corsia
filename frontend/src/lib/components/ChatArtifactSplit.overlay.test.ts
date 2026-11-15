import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('./ChatPanel.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/ChatPanelWithCard.svelte')).default }));
vi.mock('./ArtifactLeaf.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/LeafModeProbe.svelte')).default }));
vi.mock('./ArtifactTabSheet.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./CanvasSplitDivider.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));

import ChatArtifactSplit from './ChatArtifactSplit.svelte';
import { measureCanvasAs } from '$lib/__tests__/fixtures/measuredCanvas';
import { paneOverlay } from '$lib/stores/artifactCanvas';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => {
	if (component) unmount(component);
	host?.remove();
	paneOverlay.set(false);
	threadWorkspaces.set({});
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

it('uses the unobscured width for compact layout and restores the split after the overlay closes', () => {
	measureCanvasAs(1000);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ChatArtifactSplit, { target: host, props: { threadId: 't1', isDraggingOver: false, onOpenLightbox: vi.fn(), onAutoCreateThread: async () => {} } });
	flushSync();
	host.querySelector<HTMLButtonElement>('button.tile')!.click();
	flushSync();
	expect(host.querySelector('.split')!.classList.contains('compact')).toBe(false);
	paneOverlay.set(true);
	flushSync();
	expect(host.querySelector('.split')!.classList.contains('compact')).toBe(true);
	expect(host.querySelector('.leaf-probe')!.getAttribute('data-leaf-mode')).toBe('compact');
	paneOverlay.set(false);
	flushSync();
	expect(host.querySelector('.split')!.classList.contains('compact')).toBe(false);
	expect(host.querySelector('.leaf-probe')!.getAttribute('data-leaf-mode')).toBe('expanded');
});

/**
 * The browser reports a content-box resize by default. When the compact padding that
 * reserves the overlaid pane leaves in the same re-layout that shrinks the border box,
 * the content box keeps the same width and no entry is delivered - so the border-box
 * measure the split keeps must not be left holding the box from before the change.
 */
it('re-measures a shrunken canvas when the overlay padding leaves with the content box unchanged', () => {
	// Window 1100, sidebar collapsed, pane overlaid: the split box is 1100 with the
	// 312 px overlay reservation as its inline-end padding, so 788 px stays visible.
	const realBox = { border: 1100, padEnd: 312 };
	type Reg = { box: 'content-box' | 'border-box'; last: number | null };
	const regs: Reg[] = [];
	let callback: ResizeObserverCallback | null = null;
	/** The frame-end delivery step: only a registration whose own box changed reports. */
	const report = () => {
		const entries: ResizeObserverEntry[] = [];
		for (const reg of regs) {
			const size = reg.box === 'border-box' ? realBox.border : realBox.border - realBox.padEnd;
			if (reg.last === size) continue;
			reg.last = size;
			entries.push({
				contentRect: { width: size, height: 0 } as DOMRectReadOnly,
				contentBoxSize: [{ inlineSize: size, blockSize: 0 }],
				borderBoxSize: [{ inlineSize: size, blockSize: 0 }],
			} as unknown as ResizeObserverEntry);
		}
		if (entries.length) callback?.(entries, {} as ResizeObserver);
	};
	vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function measured(this: Element) {
		const width = this.classList.contains('split') ? realBox.border : 0;
		return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0, width, height: 0, toJSON: () => ({}) } as unknown as DOMRect;
	});
	vi.stubGlobal('ResizeObserver', class BoxHonestResizeObserver {
		constructor(cb: ResizeObserverCallback) { callback = cb; }

		observe(_target: Element, options?: ResizeObserverOptions): void {
			regs.push({ box: options?.box === 'border-box' ? 'border-box' : 'content-box', last: null });
			report(); // a newly observed target is reported on the next frame
		}

		unobserve(): void {}

		disconnect(): void { regs.length = 0; }
	} as unknown as typeof ResizeObserver);

	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ChatArtifactSplit, { target: host, props: { threadId: 't1', isDraggingOver: false, onOpenLightbox: vi.fn(), onAutoCreateThread: async () => {} } });
	flushSync();
	host.querySelector<HTMLButtonElement>('button.tile')!.click();
	paneOverlay.set(true);
	flushSync();
	const leafCol = () => host.querySelector<HTMLElement>('.split > .leaf-col')!;
	expect(leafCol().style.width).toBe('360px'); // 788 px visible: 788 - 420 chat - 8 divider

	// The reviewer's path: the pane returns to the flow as the sidebar collapses, so
	// the canvas box becomes 788 while its overlay reservation leaves. The content box
	// is 788 before and after, so the browser reports no content-box resize at all.
	realBox.border = 788;
	realBox.padEnd = 0;
	paneOverlay.set(false);
	flushSync();
	report();
	flushSync();
	expect(host.querySelector('.split')!.classList.contains('compact')).toBe(false);
	expect(host.querySelector('.leaf-probe')!.getAttribute('data-leaf-mode')).toBe('expanded');
	// The artifact may take at most what a 788 px canvas leaves after chat's minimum.
	expect(leafCol().style.width).toBe('360px');
});
