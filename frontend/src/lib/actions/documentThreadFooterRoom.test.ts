import { afterEach, expect, it, vi } from 'vitest';
import { documentThreadFooterRoom } from './documentThreadFooterRoom';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

it('moves only a cramped thread footer and restores it when the drawer grows', async () => {
	let resized = () => {};
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe() {}
		disconnect() {}
	});
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	const drawer = document.createElement('section'); drawer.style.rowGap = '4px';
	const heading = document.createElement('div'); heading.className = 'drawer-heading';
	const back = document.createElement('div'); back.className = 'thread-back';
	const list = document.createElement('div'); list.className = 'drawer-comments';
	const footer = document.createElement('div'); footer.className = 'drawer-foot';
	for (const [node, height] of [[heading, 48], [back, 44], [footer, 65]] as const) node.getBoundingClientRect = () => ({ height }) as DOMRect;
	let height = 147;
	Object.defineProperty(drawer, 'clientHeight', { get: () => height });
	drawer.append(heading, back, list, footer); document.body.append(drawer);
	const changed = vi.fn();
	const action = documentThreadFooterRoom(drawer, changed);
	expect(changed).toHaveBeenLastCalledWith(true);
	list.append(footer); await Promise.resolve();
	expect(changed).toHaveBeenLastCalledWith(true);
	height = 300; resized();
	expect(changed).toHaveBeenLastCalledWith(false);
	drawer.append(footer); back.remove(); await Promise.resolve();
	height = 100; resized();
	expect(changed).toHaveBeenLastCalledWith(false);
	action.destroy();
});

it('scrolls the footer exactly when the fixed rows and one reply target no longer fit, and the target is the token\'s size', () => {
	let resized = () => {};
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe() {}
		disconnect() {}
	});
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	const drawer = document.createElement('section'); drawer.style.rowGap = '4px'; drawer.style.setProperty('--touch-target', '44px');
	const heading = document.createElement('div'); heading.className = 'drawer-heading';
	const back = document.createElement('div'); back.className = 'thread-back';
	const list = document.createElement('div'); list.className = 'drawer-comments';
	const footer = document.createElement('div'); footer.className = 'drawer-foot';
	for (const [node, height] of [[heading, 48], [back, 44], [footer, 65]] as const) node.getBoundingClientRect = () => ({ height }) as DOMRect;
	let height = 0;
	Object.defineProperty(drawer, 'clientHeight', { get: () => height });
	drawer.append(heading, back, list, footer); document.body.append(drawer);
	const changed = vi.fn();
	const action = documentThreadFooterRoom(drawer, changed);
	// Fixed rows 48 + 44, footer 65, three 4px gaps (two rows plus one) and one 44px target: 213px.
	height = 213; resized();
	expect(changed).toHaveBeenLastCalledWith(false);
	height = 212; resized();
	expect(changed).toHaveBeenLastCalledWith(true);
	// A larger token raises the threshold by the same amount.
	drawer.style.setProperty('--touch-target', '56px');
	height = 224; resized();
	expect(changed).toHaveBeenLastCalledWith(true);
	height = 225; resized();
	expect(changed).toHaveBeenLastCalledWith(false);
	action.destroy();
});

it('scrolls a list\'s footer and Accept all while the keyboard is up, and puts them back when it goes down', () => {
	let resized = () => {};
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe() {}
		disconnect() {}
	});
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	const root = document.documentElement.style;
	// The list has no Back, and ample room: only the keyboard moves its footer.
	const drawer = document.createElement('section');
	const heading = document.createElement('div'); heading.className = 'drawer-heading';
	const accept = document.createElement('div'); accept.className = 'list-accept-all';
	const list = document.createElement('div'); list.className = 'drawer-comments';
	const footer = document.createElement('div'); footer.className = 'drawer-foot';
	Object.defineProperty(drawer, 'clientHeight', { get: () => 600 });
	drawer.append(heading, accept, list, footer); document.body.append(drawer);
	const changed = vi.fn();
	const action = documentThreadFooterRoom(drawer, changed);
	// No viewport reading yet (the page has not set one), and a viewport as tall as the window: no keyboard.
	expect(changed).toHaveBeenLastCalledWith(false);
	root.setProperty('--doc-visual-viewport-height', `${window.innerHeight}px`); resized();
	expect(changed).toHaveBeenLastCalledWith(false);
	// A keyboard's worth is gone from the visual viewport.
	root.setProperty('--doc-visual-viewport-height', `${window.innerHeight - 300}px`); resized();
	expect(changed).toHaveBeenLastCalledWith(true);
	// Moving Accept all into the scroller changes nothing about the keyboard, so the footer stays where it went.
	list.append(accept); resized();
	expect(changed).toHaveBeenLastCalledWith(true);
	// A browser bar's worth is not a keyboard.
	root.setProperty('--doc-visual-viewport-height', `${window.innerHeight - 60}px`); resized();
	expect(changed).toHaveBeenLastCalledWith(false);
	root.removeProperty('--doc-visual-viewport-height');
	action.destroy();
});
