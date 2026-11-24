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
