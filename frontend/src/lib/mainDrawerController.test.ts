import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createMainDrawerController } from './mainDrawerController';
import { watchInputBarHeight } from './components/inputBarHelpers';

/**
 * Issue #1321: after dismissing the composer's + action sheet, the mobile FAB
 * stack stayed offset because `--input-bar-height` did not settle back down.
 *
 * `mainDrawerController`'s transitionstart/end handlers are bound once on the
 * app shell's `<main>` element and gate a global "drawer is animating" flag
 * that `watchInputBarHeight`'s ResizeObserver checks before publishing a new
 * height. `transitionstart`/`transitionend` bubble, so any `transform`
 * transition on a *descendant* of `<main>` — e.g. the composer's plus-icon
 * rotating shut, or a sheet-trigger chevron — incorrectly armed the same
 * gate the sidebar/pane slide uses. If the ResizeObserver callback for the
 * sheet-closing resize landed inside that armed window, the height update
 * was skipped, and because ResizeObserver only re-fires on an actual size
 * change, nothing ever corrected it.
 */
describe('drawer-animating gate scopes to the drawer, not bubbled descendants', () => {
	let realRO: typeof ResizeObserver;
	let mainEl: HTMLElement;
	let plusIcon: HTMLElement;
	let floater: HTMLElement;
	let floaterHeight = 0;
	let observerCallback: ResizeObserverCallback | null = null;
	const observed: Element[] = [];

	beforeEach(() => {
		realRO = globalThis.ResizeObserver;
		globalThis.ResizeObserver = class {
			constructor(cb: ResizeObserverCallback) { observerCallback = cb; }
			observe(element: Element) { observed.push(element); }
			disconnect() {}
		} as unknown as typeof ResizeObserver;

		mainEl = document.createElement('main');
		document.body.appendChild(mainEl);
		plusIcon = document.createElement('span');
		plusIcon.className = 'plus-icon';
		mainEl.appendChild(plusIcon);
		floater = document.createElement('div');
		floater.className = 'input-floater';
		mainEl.appendChild(floater);
		Object.defineProperty(floater, 'offsetHeight', {
			configurable: true, get: () => floaterHeight,
		});

		const drawer = createMainDrawerController({
			getMainEl: () => mainEl,
			getSidebarOpen: () => false,
			setSidebarOpen: () => {},
			getPaneOpen: () => false,
			setPaneOpen: () => {},
			setPaneDragging: () => {},
		});
		mainEl.addEventListener('transitionstart', drawer.onTransitionStart);
		mainEl.addEventListener('transitionend', drawer.onTransitionEnd);

		watchInputBarHeight(() => floater, (fn) => fn());
	});

	afterEach(() => {
		globalThis.ResizeObserver = realRO;
		observed.length = 0;
		observerCallback = null;
		mainEl.remove();
		delete document.documentElement.dataset.drawerAnimating;
	});

	function fireResize() {
		observerCallback?.(
			observed.map((target) => ({ target }) as ResizeObserverEntry),
			{} as ResizeObserver,
		);
	}

	function currentPublishedHeight(): string {
		return mainEl.style.getPropertyValue('--input-bar-height');
	}

	it('publishes the real floater height after the sheet-closing transform transition bubbles from a descendant icon', () => {
		floaterHeight = 211;
		fireResize();
		expect(currentPublishedHeight()).toBe('211px');

		// The + action sheet closes: .plus-icon rotates back via its own
		// `transition: transform`, and the floater shrinks to its resting
		// height in the same tick — exactly what a completed rapid dismiss
		// produces on dev (c3fcfcaa): sheetOpen=false, actual height 110px.
		floaterHeight = 110;
		plusIcon.dispatchEvent(new TransitionEvent('transitionstart', {
			bubbles: true, cancelable: true, propertyName: 'transform',
		}));
		fireResize();

		expect(currentPublishedHeight()).toBe('110px');
	});

	it('still gates the ResizeObserver during the drawer\'s own slide transform', () => {
		// The legitimate case this gate exists for: `.main` itself transitions
		// (sidebar/pane slide) and must still suppress a stale-height write
		// mid-slide.
		floaterHeight = 211;
		fireResize();
		expect(currentPublishedHeight()).toBe('211px');

		floaterHeight = 110;
		mainEl.dispatchEvent(new TransitionEvent('transitionstart', {
			bubbles: true, cancelable: true, propertyName: 'transform',
		}));
		fireResize();

		expect(currentPublishedHeight()).toBe('211px');
	});
});

/**
 * touchend/touchcancel fire at the touch's original target even after that
 * node was removed from the DOM, and then propagate only through the
 * detached subtree. When live content re-renders the node under the finger
 * mid-swipe, `<main>` never sees the end of the drag, so the inline drag
 * transform stuck and beat the `.main.open` class transform: the drawer
 * state flipped but nothing visibly opened.
 */
describe('a swipe interrupted by a detached touch target still ends', () => {
	let mainEl: HTMLElement;
	let child: HTMLElement;
	let sidebarOpen: boolean;
	let realWidth: number;

	function touch(type: string, x: number, y: number, target: HTMLElement) {
		const t = { clientX: x, clientY: y, identifier: 1, target };
		const ev = new Event(type, { bubbles: true, cancelable: true });
		const ending = type === 'touchend' || type === 'touchcancel';
		Object.defineProperty(ev, 'touches', { value: ending ? [] : [t] });
		target.dispatchEvent(ev);
	}

	beforeEach(() => {
		realWidth = window.innerWidth;
		Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 400 });
		sidebarOpen = false;
		mainEl = document.createElement('main');
		child = document.createElement('p');
		mainEl.appendChild(child);
		document.body.appendChild(mainEl);
		const drawer = createMainDrawerController({
			getMainEl: () => mainEl,
			getSidebarOpen: () => sidebarOpen,
			setSidebarOpen: (open) => { sidebarOpen = open; },
			getPaneOpen: () => false,
			setPaneOpen: () => {},
			setPaneDragging: () => {},
		});
		mainEl.addEventListener('touchstart', drawer.onTouchStart);
		mainEl.addEventListener('touchend', drawer.onTouchEnd);
		mainEl.addEventListener('touchcancel', drawer.onTouchEnd);
		drawer.attachTouchMove(mainEl);
	});

	afterEach(() => {
		Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: realWidth });
		mainEl.remove();
	});

	for (const endType of ['touchend', 'touchcancel']) {
		it(`clears the drag transform when ${endType} fires on the detached target`, () => {
			touch('touchstart', 10, 100, child);
			touch('touchmove', 150, 102, child);
			expect(mainEl.style.transform).toBe('translateX(140px)');

			child.remove();
			touch(endType, 150, 102, child);

			expect(mainEl.style.transform).toBe('');
			expect(mainEl.style.transition).toBe('');
		});
	}

	// The phone card rounds its left edge only while it is moved aside (HomeShell.svelte),
	// so a drag marks which drawer is moving and the end of the drag clears it.
	it('marks a sidebar drag on main until the drag ends', () => {
		expect(mainEl.dataset.drawerDrag).toBeUndefined();
		touch('touchstart', 10, 100, child);
		touch('touchmove', 150, 102, child);
		expect(mainEl.dataset.drawerDrag).toBe('sidebar');

		touch('touchend', 150, 102, child);
		expect(mainEl.dataset.drawerDrag).toBeUndefined();
	});

	it('marks a pane drag from the right edge as a pane drag', () => {
		touch('touchstart', 390, 100, child);
		touch('touchmove', 250, 102, child);
		expect(mainEl.dataset.drawerDrag).toBe('pane');

		touch('touchend', 250, 102, child);
		expect(mainEl.dataset.drawerDrag).toBeUndefined();
	});

	it('leaves main unmarked when a touch never becomes a drag', () => {
		touch('touchstart', 10, 100, child);
		touch('touchmove', 12, 140, child);
		touch('touchend', 12, 140, child);
		expect(mainEl.dataset.drawerDrag).toBeUndefined();
	});
});
