import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createToastClearanceStore } from './toastClearance';

class ResizeObserverMock {
	static instances: ResizeObserverMock[] = [];
	private readonly callback: ResizeObserverCallback;

	constructor(callback: ResizeObserverCallback) {
		this.callback = callback;
		ResizeObserverMock.instances.push(this);
	}

	observe = vi.fn();
	disconnect = vi.fn();

	trigger() {
		this.callback([], this as unknown as ResizeObserver);
	}
}

function surface(top: number): HTMLElement {
	const node = document.createElement('div');
	document.body.append(node);
	vi.spyOn(node, 'getBoundingClientRect').mockImplementation(
		() => ({ top, bottom: window.innerHeight, width: 0, height: window.innerHeight - top }) as DOMRect,
	);
	return node;
}

describe('toast clearance', () => {
	beforeEach(() => {
		ResizeObserverMock.instances = [];
		vi.stubGlobal('ResizeObserver', ResizeObserverMock);
		Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
	});

	afterEach(() => {
		document.body.replaceChildren();
		vi.unstubAllGlobals();
	});

	it('registers the visible surface occupancy', () => {
		const clearance = createToastClearanceStore();
		const unregister = clearance.register(surface(620));

		expect(get(clearance)).toBe(180);

		unregister();
		expect(get(clearance)).toBe(0);
	});

	it('uses the largest of several registered surfaces and unregisters cleanly', () => {
		const clearance = createToastClearanceStore();
		const unregisterComposer = clearance.register(surface(620));
		const unregisterSheet = clearance.register(surface(360));

		expect(get(clearance)).toBe(440);

		unregisterSheet();
		expect(get(clearance)).toBe(180);
		unregisterComposer();
		expect(get(clearance)).toBe(0);
	});

	it('updates when a registered surface resizes', () => {
		const clearance = createToastClearanceStore();
		let top = 620;
		const node = surface(top);
		vi.mocked(node.getBoundingClientRect).mockImplementation(
			() => ({ top, bottom: window.innerHeight, width: 0, height: window.innerHeight - top }) as DOMRect,
		);
		const unregister = clearance.register(node);

		top = 480;
		ResizeObserverMock.instances[0].trigger();

		expect(get(clearance)).toBe(320);
		unregister();
	});
});
