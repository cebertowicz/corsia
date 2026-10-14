import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { createToastTopStore } from './toastTop';

function surface(bottom: number, height = 40): HTMLElement {
	const node = document.createElement('div');
	node.getBoundingClientRect = () => ({ bottom, height }) as DOMRect;
	return node;
}

describe('toastTop', () => {
	const stops: Array<() => void> = [];
	afterEach(() => {
		while (stops.length) stops.pop()!();
	});

	it('sits under the lowest surface and follows one that goes', () => {
		const store = createToastTopStore();
		const header = surface(56);
		const banner = surface(96);
		stops.push(store.register(header));
		expect(get(store)).toBe(56);
		const stopBanner = store.register(banner);
		expect(get(store)).toBe(96);
		stopBanner();
		expect(get(store)).toBe(56);
	});

	it('ignores a surface that takes no room', () => {
		const store = createToastTopStore();
		stops.push(store.register(surface(56)));
		stops.push(store.register(surface(300, 0)));
		expect(get(store)).toBe(56);
	});

	it('is zero with nothing registered', () => {
		expect(get(createToastTopStore())).toBe(0);
	});
});
