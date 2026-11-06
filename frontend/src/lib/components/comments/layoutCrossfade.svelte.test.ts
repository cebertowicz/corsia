import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
import { writable } from 'svelte/store';

const reduced = vi.hoisted(() => ({ store: null as unknown as ReturnType<typeof writable<boolean>> }));
vi.mock('$lib/stores/viewport', async (importOriginal) => {
	const { writable: make } = await import('svelte/store');
	reduced.store = make(true);
	return { ...await importOriginal<object>(), prefersReducedMotion: reduced.store };
});
import { useLayoutCrossfade } from './layoutCrossfade.svelte';

type Played = { node: Element; keyframes: Keyframe[]; animation: Animation & { fire: () => void; cancel: ReturnType<typeof vi.fn> } };
let played: Played[];
let cleanup: (() => void) | null = null;

/** An animation that runs until the test fires its finish. */
function animation(): Played['animation'] {
	const listeners: (() => void)[] = [];
	return {
		cancel: vi.fn(), playState: 'running', finished: new Promise(() => {}),
		addEventListener: (type: string, listener: () => void) => { if (type === 'finish') listeners.push(listener); },
		fire: () => listeners.forEach((listener) => listener()),
	} as unknown as Played['animation'];
}

function host() {
	const panel = document.createElement('div');
	panel.className = 'drawer-panel';
	const anchor = document.createElement('textarea');
	panel.append(anchor);
	document.body.append(panel);
	panel.getBoundingClientRect = () => ({ left: 0, top: 10, width: 300, height: 120 }) as DOMRect;
	return { panel, anchor };
}

function mountCrossfade(anchor: HTMLElement) {
	const state = $state({ key: 'a' });
	cleanup = $effect.root(() => { useLayoutCrossfade(() => anchor, () => state.key); });
	flushSync();
	return state;
}

const copies = () => Array.from(document.body.querySelectorAll<HTMLElement>('[aria-hidden="true"]'));

beforeEach(() => {
	played = [];
	reduced.store.set(true);
	vi.spyOn(Element.prototype, 'animate').mockImplementation(function (this: Element, keyframes) {
		const item = animation();
		played.push({ node: this, keyframes: keyframes as Keyframe[], animation: item });
		return item;
	});
});
afterEach(() => { cleanup?.(); cleanup = null; vi.restoreAllMocks(); document.body.innerHTML = ''; });

describe('useLayoutCrossfade', () => {
	it('lays a copy of the host over it and crossfades the copy out and the host in', async () => {
		const { panel, anchor } = host();
		const state = mountCrossfade(anchor);
		state.key = 'b'; flushSync(); await tick();
		expect(copies()).toHaveLength(1);
		const copy = copies()[0];
		expect(copy.inert).toBe(true);
		expect(played.map(({ node, keyframes }) => [node, keyframes])).toEqual([
			[copy, [{ opacity: 1 }, { opacity: 0 }]],
			[panel, [{ opacity: 0 }, { opacity: 1 }]],
		]);
		played[0].animation.fire();
		expect(copies()).toHaveLength(0);
	});

	it('keeps the copy on screen when a change lands mid-fade and starts again from the opacity it reached', async () => {
		const { panel, anchor } = host();
		const state = mountCrossfade(anchor);
		state.key = 'b'; flushSync(); await tick();
		const copy = copies()[0];
		copy.style.opacity = '0.6';
		state.key = 'c'; flushSync(); await tick();
		expect(copies()).toEqual([copy]);
		expect(played[0].animation.cancel).toHaveBeenCalled();
		expect(played[1].animation.cancel).toHaveBeenCalled();
		expect(played.slice(2).map(({ node, keyframes }) => [node, keyframes])).toEqual([
			[copy, [{ opacity: 0.6 }, { opacity: 0 }]],
			[panel, [{ opacity: 0.4 }, { opacity: 1 }]],
		]);
		// The first fade's finish no longer removes the copy the second fade is still showing.
		played[0].animation.fire();
		expect(copies()).toEqual([copy]);
	});

	it('copies the layout afresh once the last fade has finished', async () => {
		const { anchor } = host();
		const state = mountCrossfade(anchor);
		state.key = 'b'; flushSync(); await tick();
		const first = copies()[0];
		played[0].animation.fire();
		state.key = 'c'; flushSync(); await tick();
		expect(copies()).toHaveLength(1);
		expect(copies()[0]).not.toBe(first);
		expect(played[2].keyframes).toEqual([{ opacity: 1 }, { opacity: 0 }]);
	});

	it('does nothing with normal motion, where the row eases in on its own', async () => {
		reduced.store.set(false);
		const { anchor } = host();
		const state = mountCrossfade(anchor);
		state.key = 'b'; flushSync(); await tick();
		expect(copies()).toHaveLength(0);
		expect(played).toHaveLength(0);
	});

	it('removes its copy when the component goes away mid-fade', async () => {
		const { anchor } = host();
		const state = mountCrossfade(anchor);
		state.key = 'b'; flushSync(); await tick();
		cleanup?.(); cleanup = null;
		expect(copies()).toHaveLength(0);
	});
});
