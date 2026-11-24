import { afterEach, expect, it, vi } from 'vitest';
import { documentFooterRoom } from './documentFooterRoom';

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
	const action = documentFooterRoom(drawer, changed);
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
	const action = documentFooterRoom(drawer, changed);
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

it('scrolls a list\'s footer while a text field in it has focus on a touch screen, and puts it back when focus leaves', () => {
	vi.stubGlobal('ResizeObserver', class { constructor() {} observe() {} disconnect() {} });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	let touch = true;
	vi.stubGlobal('matchMedia', (query: string) => ({ matches: touch && query === '(pointer: coarse)' }));
	// The list has no Back, and ample room: only the keyboard moves its footer. The window is never resized: a browser that
	// shrinks the layout with the keyboard (`interactive-widget=resizes-content`) leaves no gap to measure.
	const drawer = document.createElement('section');
	const heading = document.createElement('div'); heading.className = 'drawer-heading';
	const list = document.createElement('div'); list.className = 'drawer-comments';
	const reply = document.createElement('textarea');
	const like = document.createElement('button');
	const footer = document.createElement('div'); footer.className = 'drawer-foot';
	Object.defineProperty(drawer, 'clientHeight', { get: () => 600 });
	list.append(reply, like); drawer.append(heading, list, footer); document.body.append(drawer);
	const changed = vi.fn();
	const action = documentFooterRoom(drawer, changed);
	expect(changed).toHaveBeenLastCalledWith(false);
	// A button is not a text field: no keyboard opens for it.
	like.focus(); drawer.dispatchEvent(new FocusEvent('focusin'));
	expect(changed).toHaveBeenLastCalledWith(false);
	reply.focus(); drawer.dispatchEvent(new FocusEvent('focusin'));
	expect(changed).toHaveBeenLastCalledWith(true);
	// Another field of the page outside the drawer takes focus: the drawer's footer goes back.
	const outside = document.createElement('input'); document.body.append(outside);
	outside.focus(); drawer.dispatchEvent(new FocusEvent('focusout'));
	expect(changed).toHaveBeenLastCalledWith(false);
	// A fine pointer (a desktop with a physical keyboard) never moves it.
	touch = false;
	reply.focus(); drawer.dispatchEvent(new FocusEvent('focusin'));
	expect(changed).toHaveBeenLastCalledWith(false);
	action.destroy();
});
