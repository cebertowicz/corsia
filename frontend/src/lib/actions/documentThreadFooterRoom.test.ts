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
