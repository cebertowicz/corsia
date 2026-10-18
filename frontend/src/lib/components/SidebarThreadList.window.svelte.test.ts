import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SidebarThreadList from './SidebarThreadList.svelte';
import { WINDOW_THRESHOLD } from './sidebarWindow';
import { threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';

// The windowing runtime over a list well past WINDOW_THRESHOLD, inside a real scrolling ancestor. jsdom has no
// layout, so the viewport is faked: a 600px scroller whose pane reports its top as -scrollTop. Rows are
// unmeasured here (ResizeObserver is a no-op), so the runtime uses its 40px default row height.
const ROWS = 408;
const VIEWPORT = 600;
const MOUNT_BOUND = 60; // about 15 visible rows + overscan on both sides; far below ROWS

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

class NoopResizeObserver {
	observe() {}
	unobserve() {}
	disconnect() {}
}

function render(count: number) {
	threads.set(Array.from({ length: count }, (_, i) => ({
		id: `t${i}`, user_id: 'u', profile: 'personal', title: `Thread ${i}`, model: null,
		created_at: new Date(Date.now() - i * 1000).toISOString(), updated_at: new Date(Date.now() - i * 1000).toISOString(),
	})));
	uiScope.set(null);
	host = document.createElement('div');
	host.style.overflowY = 'auto';
	Object.defineProperty(host, 'clientHeight', { configurable: true, value: VIEWPORT });
	document.body.appendChild(host);
	// The pane mounts inside the scroller; report its top relative to the scroll position.
	const proto = HTMLElement.prototype.getBoundingClientRect;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		const rect = proto.call(this);
		if (this.classList.contains('scope-pane') && host) return { ...rect, top: -host.scrollTop } as DOMRect;
		return rect;
	});
	component = mount(SidebarThreadList, {
		target: host,
		props: { onSelectThread: () => {}, onDeleteThread: () => {}, onOpenContextMenu: () => {} },
	});
	flushSync();
	return host;
}

async function settle() {
	await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
	await tick();
	flushSync();
}

async function scrollTo(root: HTMLElement, y: number) {
	await settle(); // let the pane attach (its action runs after mount) before the scroll, as a real user scroll would follow it
	root.scrollTop = y;
	root.dispatchEvent(new Event('scroll'));
	await settle();
}

const mountedPositions = (root: HTMLElement) =>
	[...root.querySelectorAll('[role="listitem"]')].map((el) => Number(el.getAttribute('aria-posinset')));

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', NoopResizeObserver);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	host = null;
	threads.set([]);
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('windowed sidebar list past the threshold', () => {
	it('mounts only the rows near the top of a 408-row list and reports the full set size', () => {
		expect(ROWS).toBeGreaterThan(WINDOW_THRESHOLD);
		const root = render(ROWS);
		const positions = mountedPositions(root);
		expect(positions.length).toBeGreaterThan(VIEWPORT / 40 / 2);
		expect(positions.length).toBeLessThan(MOUNT_BOUND);
		expect(positions[0]).toBe(1);
		expect(root.querySelector('[aria-posinset="1"]')?.getAttribute('aria-setsize')).toBe(String(ROWS));
		expect(root.querySelector('.win-gap')).not.toBeNull();
	});

	it('moves the mounted window with the scroll position and keeps the DOM bounded', async () => {
		const root = render(ROWS);
		await scrollTo(root, 8000);
		const positions = mountedPositions(root);
		expect(positions.length).toBeLessThan(MOUNT_BOUND);
		expect(positions).not.toContain(1);
		// 8000px at 40px a row (plus a label) is about row 200
		expect(positions.some((p) => p > 150 && p < 215)).toBe(true);
		expect(root.textContent).not.toContain('Thread 0');
	});

	it('keeps the focused row mounted while the window scrolls away from it', async () => {
		const root = render(ROWS);
		const focused = root.querySelector('[aria-posinset="3"]') as HTMLElement;
		focused.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
		flushSync();
		await scrollTo(root, 8000);
		const positions = mountedPositions(root);
		expect(positions.some((p) => p > 150)).toBe(true); // the window did move to the new place
		expect(positions).toContain(3); // and the focused row stayed mounted
		expect(positions.length).toBeLessThan(MOUNT_BOUND + 10);
	});
});
