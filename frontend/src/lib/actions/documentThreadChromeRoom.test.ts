import { afterEach, expect, it, vi } from 'vitest';
import { COMPOSER_ROOM, documentThreadChromeRoom } from './documentThreadChromeRoom';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

function sheet() {
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
	const button = document.createElement('button'); button.className = 'drawer-back'; back.append(button);
	const list = document.createElement('div'); list.className = 'drawer-comments';
	Object.defineProperty(back, 'offsetHeight', { value: 44 });
	Object.defineProperty(button, 'offsetWidth', { value: 53 });
	const room = { value: 47 };
	Object.defineProperty(list, 'clientHeight', { get: () => room.value });
	drawer.append(heading, back, list); document.body.append(drawer);
	return { drawer, back, room, resized: () => resized() };
}

it('puts Back on the heading row only while the scroller is shorter than the composer needs, and restores it', () => {
	const { drawer, room, resized } = sheet();
	const action = documentThreadChromeRoom(drawer);
	expect(drawer.hasAttribute('data-short-chrome')).toBe(true);
	expect(drawer.style.getPropertyValue('--back-room')).toBe('53px');
	// With Back's 44px row and 4px gap counted back in, 99 - 48 = 51 is still short.
	room.value = 99; resized();
	expect(drawer.hasAttribute('data-short-chrome')).toBe(true);
	// 144 - 48 = 96 is exactly the composer's room, so the normal layout returns.
	room.value = COMPOSER_ROOM + 48; resized();
	expect(drawer.hasAttribute('data-short-chrome')).toBe(false);
	expect(drawer.style.getPropertyValue('--back-room')).toBe('');
	// A tall sheet never changes.
	room.value = 400; resized();
	expect(drawer.hasAttribute('data-short-chrome')).toBe(false);
	action.destroy();
});

it('does nothing in the list view, where there is no Back', () => {
	const { drawer, back, resized } = sheet();
	back.remove();
	const action = documentThreadChromeRoom(drawer);
	resized();
	expect(drawer.hasAttribute('data-short-chrome')).toBe(false);
	action.destroy();
});
