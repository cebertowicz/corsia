import { afterEach, expect, it, vi } from 'vitest';
import { documentHalfSheetRoom } from './documentHalfSheetRoom';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

it('tracks the fixed delivery row, its gap, changes and cleanup', async () => {
	let resized = () => {};
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe() {}
		disconnect() {}
	});
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	const panel = document.createElement('div'); panel.className = 'drawer-panel';
	const drawer = document.createElement('section'); drawer.style.rowGap = '4px';
	const list = document.createElement('div'); list.className = 'drawer-comments';
	const footer = document.createElement('div'); footer.className = 'drawer-foot';
	let footerHeight = 65;
	footer.getBoundingClientRect = () => ({ height: footerHeight }) as DOMRect;
	drawer.append(list, footer); panel.append(drawer); document.body.append(panel);
	const action = documentHalfSheetRoom(drawer)!;
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('69px');
	footerHeight = 100; resized();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('104px');
	// The footer scrolls inside the list while the keyboard is up; its room is still the half sheet's.
	list.append(footer); await Promise.resolve();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('104px');
	list.remove(); await Promise.resolve();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('0px');
	action.destroy();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('');
});

it('adds the heading\'s second row when the switch wraps under the title, and nothing while it is one row', () => {
	let resized = () => {};
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe() {}
		disconnect() {}
	});
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	const panel = document.createElement('div'); panel.className = 'drawer-panel';
	const drawer = document.createElement('section');
	const heading = document.createElement('div'); heading.className = 'drawer-heading'; heading.style.paddingBottom = '4px';
	const tools = document.createElement('div'); tools.className = 'drawer-tools';
	let headingHeight = 48;
	heading.getBoundingClientRect = () => ({ height: headingHeight }) as DOMRect;
	tools.getBoundingClientRect = () => ({ height: 44 }) as DOMRect;
	heading.append(tools); drawer.append(heading); panel.append(drawer); document.body.append(panel);
	const action = documentHalfSheetRoom(drawer)!;
	expect(panel.style.getPropertyValue('--drawer-half-extra'), 'one row: 44 + 4 padding').toBe('0px');
	headingHeight = 92; resized();
	expect(panel.style.getPropertyValue('--drawer-half-extra'), 'wrapped: the second 44 px row').toBe('44px');
	headingHeight = 48; resized();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('0px');
	action.destroy();
});

it('counts no status row: the sheet has none, its switch is in the heading', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	const panel = document.createElement('div'); panel.className = 'drawer-panel';
	const drawer = document.createElement('section'); drawer.style.rowGap = '4px';
	const status = document.createElement('div'); status.className = 'drawer-switch';
	status.getBoundingClientRect = () => ({ height: 44 }) as DOMRect;
	drawer.append(status); panel.append(drawer); document.body.append(panel);
	const action = documentHalfSheetRoom(drawer)!;
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('0px');
	action.destroy();
});
