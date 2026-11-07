import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Mock list, deliberately the same shape as ChatArtifactSplit.cardOpen.test.ts: only
// ChatPanel's own internals and ArtifactLeaf's heavy body are replaced. SubagentChip,
// the open request, its canonical consumer, the leaf route, split layout and the
// surface store are all the real ones.
vi.mock('./ChatPanel.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/ChatPanelWithSubagentChip.svelte')).default }));
vi.mock('./ArtifactLeaf.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/LeafModeProbe.svelte')).default }));
vi.mock('./ArtifactTabSheet.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./CanvasSplitDivider.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));

import ChatArtifactSplit from './ChatArtifactSplit.svelte';
import { COMPACT_CANVAS, WIDE_CANVAS, jsdomFacts, measureCanvasAs } from '$lib/__tests__/fixtures/measuredCanvas';
import { canvasCompact } from '$lib/stores/artifactCanvas';
import {
	openSubagentConversationTab, setActiveSurface, threadWorkspaces, type ThreadArtifactWorkspace,
} from '$lib/stores/artifactWorkspace';
import { _resetSubagentHydrationForTests, registerSubagent, subagentMeta } from '$lib/stores/subagents';
import { watchSubagentOpenRequests } from '$lib/subagentOpen';

const PARENT = 't1';
const CHILD = 'child-1';

const host: { el: HTMLElement | null } = { el: null };
let component: Record<string, unknown> | null = null;
let stopConsumer: (() => void) | null = null;
let stopObserver: () => void = () => {};
let surfaces: string[] = [];
let ancestorClicks = 0;
const selectThread = vi.fn(async () => {});

function splitEl(): HTMLElement {
	return host.el!.querySelector<HTMLElement>('.split')!;
}

function chatCol(): HTMLElement {
	return host.el!.querySelector<HTMLElement>('.chat-col')!;
}

function leafCol(): HTMLElement | null {
	return host.el!.querySelector<HTMLElement>('.leaf-col');
}

function leafMode(): string | null {
	return leafCol()?.querySelector('.leaf-probe')?.getAttribute('data-leaf-mode') ?? null;
}

function chip(): HTMLButtonElement {
	const found = host.el!.querySelector<HTMLButtonElement>('button.sa-chip');
	if (!found) throw new Error('the real SubagentChip did not render in the chat column');
	return found;
}

function workspace(): ThreadArtifactWorkspace | undefined {
	return get(threadWorkspaces)[PARENT];
}

function surfaceSequence(): string {
	// 'none' is a store notify for a thread with no workspace yet (Svelte's
	// `writable.update` notifies even when the reducer returns no change), which is
	// chat by definition. The real surface sequence is what a person sees.
	return surfaces.filter((surface) => surface !== 'none').join(' -> ');
}

function rawSequence(): string {
	return surfaces.join(' -> ');
}

function mountSplit(): void {
	host.el = document.createElement('div');
	document.body.appendChild(host.el);
	// Test observation only: record every surface the product publishes, in order.
	surfaces = [];
	stopObserver = threadWorkspaces.subscribe((all) => {
		const next = all[PARENT]?.activeSurface ?? 'none';
		if (surfaces[surfaces.length - 1] !== next) surfaces.push(next);
	});
	component = mount(ChatArtifactSplit, {
		target: host.el,
		props: { threadId: PARENT, isDraggingOver: false, onOpenLightbox: vi.fn(), onAutoCreateThread: async () => {} },
	});
	flushSync();
	// The canonical consumer wiring, identical to src/routes/+page.svelte:301.
	stopConsumer = watchSubagentOpenRequests({ selectThread });
	chatCol().addEventListener('click', () => { ancestorClicks += 1; });
	surfaces = [];
}

/**
 * Presses a key on a focused control the way HTML activation behaviour defines it:
 * Enter fires the click on keydown, Space on keyup. Whether this DOM fired that click
 * by itself is reported, so the harness never claims native activation it did not get.
 */
function pressActivation(target: HTMLElement, key: string): boolean {
	let nativeClick = false;
	const record = () => { nativeClick = true; };
	target.addEventListener('click', record);
	const accepted = target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
	if (key === ' ') target.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true, cancelable: true }));
	const nativeHappened = nativeClick;
	target.removeEventListener('click', record);
	if (!nativeHappened && accepted) {
		// The activation click the browser would have fired, through the engine rather
		// than a hand-built event: `HTMLElement.click()` sets view, bubbling and
		// composed itself (proven to bubble by the probe below).
		target.click();
	}
	return nativeHappened;
}

beforeEach(() => {
	threadWorkspaces.set({});
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
	registerSubagent(PARENT, CHILD, 'Review worker', 'cc', 'unknown');
	selectThread.mockClear();
	ancestorClicks = 0;
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	stopObserver();
	stopObserver = () => {};
	if (stopConsumer) stopConsumer();
	stopConsumer = null;
	host.el?.remove();
	host.el = null;
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('harness capability, measured before any claim about it', () => {
	it('gives a focused button no native click of its own when Enter is pressed', () => {
		const button = document.createElement('button');
		document.body.appendChild(button);
		let clicks = 0;
		button.addEventListener('click', () => { clicks += 1; });
		button.focus();
		const accepted = button.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
		button.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true, cancelable: true }));
		button.remove();
		console.info(`[harness] jsdom ResizeObserver present: ${jsdomFacts.resizeObserver}; Enter -> native click: ${clicks > 0}; keydown default accepted: ${accepted}`);
		expect(clicks, 'this DOM performs no button activation behaviour').toBe(0);
	});

	it('bubbles a real chip click up to the chat column, so the ancestor handler is really reached', () => {
		measureCanvasAs(COMPACT_CANVAS);
		mountSplit();
		chip().click();
		expect(ancestorClicks, 'HTMLElement.click() bubbles in this DOM').toBeGreaterThan(0);
	});
});

describe('opening a worker from its real spawn chip', () => {
	describe('on a measured compact canvas', () => {
		beforeEach(() => measureCanvasAs(COMPACT_CANVAS));

		it('measures compact through the product flag before any interaction', () => {
			mountSplit();
			expect(splitEl().classList.contains('compact')).toBe(true);
			expect(leafMode(), 'a chat-only compact canvas shows no leaf').toBeNull();
		});

		it('leaves the worker leaf on the canvas after the chip click bubbles to the chat column', () => {
			mountSplit();
			chip().click();
			flushSync();
			console.info(`[compact click] real surfaces: ${surfaceSequence()}; raw notifies: ${rawSequence()}`);

			const ws = workspace();
			const leaf = ws?.tabs.find((tab) => tab.kind === 'subagent');
			expect(leaf?.threadId, 'canonical leaf route: the child leaf beside its parent').toBe(CHILD);
			expect(leaf?.path, 'the canonical subagent tab path').toBe(`thread:${CHILD}`);
			expect(leaf?.title, 'the worker name from the durable spawn marker').toBe('Review worker');
			expect(ws?.activeTabId, 'the new leaf is the selected tab').toBe(leaf?.instanceId);
			expect(ws?.activeSurface, 'a child-selected artifact must survive the ancestor click').toBe('artifact');
			expect(leafCol(), 'compact renders the leaf only while artifact is the surface').not.toBeNull();
			expect(leafMode()).toBe('compact');
			expect(chatCol().classList.contains('offscreen'), 'chat is hidden, not unmounted, while the artifact owns the canvas').toBe(true);
			expect(ancestorClicks, 'the click really did bubble to the chat column').toBe(1);
			expect(selectThread).toHaveBeenCalledWith(PARENT);
			expect(surfaceSequence(), 'ordered activeSurface transitions').toBe('artifact');
			expect(get(canvasCompact), 'the published compact-canvas mode').toBe(true);
		});

		it('keeps the leaf for Enter activation on the focused chip', () => {
			mountSplit();
			const target = chip();
			target.focus();
			const native = pressActivation(target, 'Enter');
			flushSync();
			console.info(`[compact Enter] native activation from this DOM: ${native}; real surfaces: ${surfaceSequence()}; raw notifies: ${rawSequence()}`);

			expect(workspace()?.activeSurface, 'Enter on the focused chip').toBe('artifact');
			expect(leafMode()).toBe('compact');
			expect(surfaceSequence(), 'ordered activeSurface transitions').toBe('artifact');
		});

		it('keeps the leaf for Space activation on the focused chip', () => {
			mountSplit();
			const target = chip();
			target.focus();
			const native = pressActivation(target, ' ');
			flushSync();
			console.info(`[compact Space] native activation from this DOM: ${native}; real surfaces: ${surfaceSequence()}; raw notifies: ${rawSequence()}`);

			expect(workspace()?.activeSurface, 'Space on the focused chip').toBe('artifact');
			expect(leafMode()).toBe('compact');
			expect(surfaceSequence(), 'ordered activeSurface transitions').toBe('artifact');
		});

		it('keeps chat on the canvas when the user clicks it with a worker leaf already open', () => {
			mountSplit();
			// Put chat on screen the way the product does after a compact back step: the
			// leaf stays, the surface is chat, so the chat column is the visible one.
			openSubagentConversationTab(PARENT, { threadId: CHILD, title: 'Review worker' });
			setActiveSurface(PARENT, 'chat');
			flushSync();
			surfaces = [];
			chatCol().click();
			flushSync();
			console.info(`[compact chat click] real surfaces: ${surfaceSequence()}; raw notifies: ${rawSequence()}`);

			expect(chatCol().classList.contains('offscreen'), 'chat is the visible column here, so this is a click a person can really make').toBe(false);
			expect(workspace()?.activeSurface, 'chat stays the visible surface').toBe('chat');
			expect(leafCol(), 'the open leaf stays mounted while chat is the surface').toBeNull();
			expect(workspace()?.tabs.some((tab) => tab.kind === 'subagent' && tab.threadId === CHILD), 'the leaf tab survives').toBe(true);
		});
	});

	describe('on a measured wide canvas — controls unchanged by the correction', () => {
		beforeEach(() => measureCanvasAs(WIDE_CANVAS));

		it('measures wide through the product flag before any interaction', () => {
			mountSplit();
			expect(splitEl().classList.contains('compact')).toBe(false);
		});

		it('gives chat the focus when the user clicks the chat column while the worker leaf is open', () => {
			mountSplit();
			chip().click();
			flushSync();
			surfaces = [];
			chatCol().click();
			flushSync();
			console.info(`[wide chat focus] real surfaces: ${surfaceSequence()}; raw notifies: ${rawSequence()}`);

			expect(workspace()?.activeSurface, 'clicking chat in wide mode still takes focus').toBe('chat');
			expect(leafCol(), 'and the open leaf stays on the canvas in wide mode').not.toBeNull();
			expect(leafMode()).toBe('expanded');
		});

		it('still renders the worker leaf when the same chip click selects chat', () => {
			mountSplit();
			chip().click();
			flushSync();
			console.info(`[wide leaf visibility] real surfaces: ${surfaceSequence()}; raw notifies: ${rawSequence()}`);

			const ws = workspace();
			expect(ws?.tabs.some((tab) => tab.kind === 'subagent' && tab.threadId === CHILD)).toBe(true);
			expect(leafCol(), 'expanded desktop renders the leaf whatever the active surface').not.toBeNull();
			expect(leafMode()).toBe('expanded');
			expect(surfaceSequence(), 'wide keeps the ancestor focus rule; only compact rendering depends on it').toBe('artifact -> chat');
		});
	});
});
