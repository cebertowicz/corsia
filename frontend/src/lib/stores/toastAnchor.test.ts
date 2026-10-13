import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createToastAnchorStore } from './toastAnchor';

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

function column(rect: { left: number; right: number }): HTMLElement {
	const node = document.createElement('div');
	document.body.append(node);
	vi.spyOn(node, 'getBoundingClientRect').mockImplementation(
		() => ({ ...rect, width: rect.right - rect.left }) as DOMRect,
	);
	return node;
}

describe('toast anchor', () => {
	beforeEach(() => {
		ResizeObserverMock.instances = [];
		vi.stubGlobal('ResizeObserver', ResizeObserverMock);
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
	});

	afterEach(() => {
		document.body.replaceChildren();
		vi.unstubAllGlobals();
	});

	it('centres on the window until a column registers', () => {
		const anchor = createToastAnchorStore();
		expect(get(anchor)).toBeNull();
	});

	it('publishes the centre and width of the registered column', () => {
		const anchor = createToastAnchorStore();
		anchor.register(column({ left: 280, right: 1440 }));
		expect(get(anchor)).toEqual({ x: 860, width: 1160 });
	});

	it('lets the chat column take over from the main area and hand back', () => {
		const anchor = createToastAnchorStore();
		const unregisterMain = anchor.register(column({ left: 280, right: 1440 }));
		const unregisterChat = anchor.register(column({ left: 280, right: 912 }));
		expect(get(anchor)).toEqual({ x: 596, width: 632 });

		unregisterChat();
		expect(get(anchor)).toEqual({ x: 860, width: 1160 });
		unregisterMain();
		expect(get(anchor)).toBeNull();
	});

	it('follows a column that resizes', () => {
		const anchor = createToastAnchorStore();
		const node = column({ left: 280, right: 1440 });
		anchor.register(node);

		vi.mocked(node.getBoundingClientRect).mockImplementation(
			() => ({ left: 0, right: 1440, width: 1440 }) as DOMRect,
		);
		ResizeObserverMock.instances[0].trigger();
		expect(get(anchor)).toEqual({ x: 720, width: 1440 });
	});

	it('re-measures when an ancestor that slides the column settles, not for unrelated nodes', () => {
		const anchor = createToastAnchorStore();
		const ancestor = document.createElement('div');
		const node = column({ left: 280, right: 1440 });
		ancestor.append(node);
		document.body.append(ancestor);
		const stranger = document.createElement('div');
		document.body.append(stranger);
		anchor.register(node);

		vi.mocked(node.getBoundingClientRect).mockImplementation(
			() => ({ left: 300, right: 1440, width: 1140 }) as DOMRect,
		);
		stranger.dispatchEvent(new Event('transitionend', { bubbles: true }));
		expect(get(anchor)).toEqual({ x: 860, width: 1160 });
		ancestor.dispatchEvent(new Event('transitionend', { bubbles: true }));
		expect(get(anchor)).toEqual({ x: 870, width: 1140 });
	});

	it('clamps a column that is partly off screen to the viewport', () => {
		const anchor = createToastAnchorStore();
		anchor.register(column({ left: 300, right: 1740 }));
		expect(get(anchor)).toEqual({ x: 870, width: 1140 });
	});

	it('ignores a column with no visible width', () => {
		const anchor = createToastAnchorStore();
		anchor.register(column({ left: 1440, right: 1700 }));
		expect(get(anchor)).toBeNull();
	});
});
