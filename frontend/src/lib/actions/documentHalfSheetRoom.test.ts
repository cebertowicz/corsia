import { afterEach, expect, it, vi } from 'vitest';
import { documentHalfSheetRoom } from './documentHalfSheetRoom';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

it('tracks the fixed rows, their gaps, changes and cleanup', async () => {
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
	const status = document.createElement('div'); status.className = 'drawer-switch';
	const footer = document.createElement('div'); footer.className = 'drawer-foot';
	let footerHeight = 65;
	status.getBoundingClientRect = () => ({ height: 44 }) as DOMRect;
	footer.getBoundingClientRect = () => ({ height: footerHeight }) as DOMRect;
	drawer.append(status, footer); panel.append(drawer); document.body.append(panel);
	const action = documentHalfSheetRoom(drawer)!;
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('117px');
	footerHeight = 100; resized();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('152px');
	status.remove(); await Promise.resolve();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('104px');
	action.destroy();
	expect(panel.style.getPropertyValue('--drawer-half-extra')).toBe('');
});
