import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createToastClearanceStore, toastClearance, toastClearanceBand } from './toastClearance';

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

	it('uses a resting measurement when a sheet is transformed at registration', () => {
		const clearance = createToastClearanceStore();
		const drawer = surface(window.innerHeight);
		Object.defineProperty(drawer, 'offsetHeight', { configurable: true, value: 440 });
		const unregister = clearance.register(drawer, { measure: () => drawer.offsetHeight });

		expect(get(clearance)).toBe(440);
		unregister();
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

describe('toast clearance band', () => {
	beforeEach(() => {
		ResizeObserverMock.instances = [];
		vi.stubGlobal('ResizeObserver', ResizeObserverMock);
		Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
	});

	afterEach(() => {
		document.body.replaceChildren();
		vi.unstubAllGlobals();
	});

	function band(childTops: number[]): HTMLElement {
		const node = document.createElement('div');
		document.body.append(node);
		for (const top of childTops) {
			const child = document.createElement('div');
			vi.spyOn(child, 'getBoundingClientRect').mockImplementation(
				() => ({ top, bottom: top + 60, height: 60, width: 300 }) as DOMRect,
			);
			node.append(child);
		}
		return node;
	}

	it('clears nothing while the band holds nothing, even though its box reserves a gutter', () => {
		const node = band([]);
		vi.spyOn(node, 'getBoundingClientRect').mockImplementation(
			() => ({ top: 600, bottom: 632, height: 32, width: 300 }) as DOMRect,
		);
		const action = toastClearanceBand(node);
		expect(get(toastClearance)).toBe(0);
		action.destroy();
	});

	it('clears up to the top of the highest occupant', () => {
		const action = toastClearanceBand(band([640, 700]));
		expect(get(toastClearance)).toBe(160);
		action.destroy();
		expect(get(toastClearance)).toBe(0);
	});
});

