import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Lightbox from './Lightbox.svelte';
import { backLayerDepth, pushBackLayer, resetBackLayers } from '$lib/historyBackLayer';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function pop(state: unknown = null): PopStateEvent {
	const event = new PopStateEvent('popstate', { state });
	vi.spyOn(event, 'stopImmediatePropagation');
	window.dispatchEvent(event);
	return event;
}

function render() {
	component = mount(Lightbox, { target: host, props: { url: '/image.png', open: true } });
	flushSync();
}

beforeEach(() => {
	resetBackLayers();
	vi.spyOn(history, 'pushState').mockImplementation(() => {});
	vi.spyOn(history, 'back').mockImplementation(() => {});
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	resetBackLayers();
	vi.restoreAllMocks();
});

describe('Lightbox back-layer integration', () => {
	it('closes before the compact artifact layer and leaves it for the next Back', async () => {
		const artifact = vi.fn(() => true);
		pushBackLayer(artifact);
		render();
		expect(backLayerDepth()).toBe(2);

		pop();
		flushSync();
		await tick();
		expect(host.querySelector('.lightbox-overlay')).toBeNull();
		expect(artifact).not.toHaveBeenCalled();
		expect(backLayerDepth()).toBe(1);

		pop();
		expect(artifact).toHaveBeenCalledOnce();
	});

	it('direct close unwinds only the lightbox entry and suppresses its cleanup pop', async () => {
		const artifact = vi.fn(() => true);
		pushBackLayer(artifact);
		const artifactState = vi.mocked(history.pushState).mock.calls[0][0];
		render();
		host.querySelector<HTMLButtonElement>('.lightbox-close')!.click();
		flushSync();
		await tick();
		expect(host.querySelector('.lightbox-overlay')).toBeNull();
		expect(history.back).toHaveBeenCalledOnce();
		expect(backLayerDepth()).toBe(1);

		pop(artifactState);
		expect(artifact).not.toHaveBeenCalled();
		expect(backLayerDepth()).toBe(1);
	});
});
