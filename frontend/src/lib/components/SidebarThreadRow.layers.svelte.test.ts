import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SidebarThreadRow from './SidebarThreadRow.svelte';
import type { Thread } from '$lib/stores/threads';

// A row's swipe transforms are composited layers; a row at rest must carry none (mounting windowed rows
// otherwise stalls the compositor), but it keeps them through a swipe, while open, and while sliding shut.

const thread: Thread = {
	id: 'thread-layers', user_id: 'steve', profile: 'default',
	title: 'A row', model: null, project_id: 'corsia',
	created_at: '2026-07-22T08:00:00', updated_at: '2026-07-22T08:00:00',
};

const props = $state({
	thread,
	isPinned: false,
	isActive: false,
	renaming: false,
	renameValue: '',
	swiping: false,
	isSwipeGesture: false,
	transformX: 0,
	leftZoneX: -80,
	rightZoneX: 144,
	pinArmed: false,
	deleteArmed: false,
	onTouchStart: () => {},
	onTouchEnd: () => {},
	onSelect: () => {},
	onOpenContextMenu: () => {},
	onPin: () => {},
	onArchive: () => {},
	onDelete: () => {},
	onStartRename: () => {},
	onFinishRename: () => {},
	onCancelRename: () => {},
});

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const wrapper = () => host.querySelector<HTMLElement>('.thread-item-wrapper')!;
const item = () => host.querySelector<HTMLElement>('.thread-item')!;
const layered = () =>
	['.thread-item', '.left-zone', '.right-zone-group'].map((s) => host.querySelector<HTMLElement>(s)!.style.transform !== '');

function setProps(next: Partial<typeof props>) {
	Object.assign(props, next);
	flushSync();
}
function endSlide(propertyName = 'transform') {
	item().dispatchEvent(Object.assign(new Event('transitionend', { bubbles: true }), { propertyName }));
	flushSync();
}
const reveal = () => setProps({ swiping: false, isSwipeGesture: false, transformX: -144, leftZoneX: -80, rightZoneX: 0 });
const close = () => setProps({ swiping: false, isSwipeGesture: false, transformX: 0, leftZoneX: -80, rightZoneX: 144 });

beforeEach(() => {
	vi.useFakeTimers();
	Object.assign(props, { swiping: false, isSwipeGesture: false, transformX: 0, leftZoneX: -80, rightZoneX: 144 });
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(SidebarThreadRow, { target: host, props });
	flushSync();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.useRealTimers();
});

describe('SidebarThreadRow swipe layers', () => {
	it('carries no transform at rest', () => {
		expect(layered()).toEqual([false, false, false]);
		expect(wrapper().classList.contains('engaged')).toBe(false);
	});

	it('takes the transforms as soon as a touch starts, before any movement', () => {
		setProps({ swiping: true });
		expect(layered()).toEqual([true, true, true]);
		expect(wrapper().classList.contains('engaged')).toBe(true);
		expect(item().style.transform).toBe('translate3d(0px, 0, 0)');
	});

	it('keeps them while a row is open', () => {
		setProps({ swiping: true });
		reveal();
		endSlide();
		vi.advanceTimersByTime(1000);
		expect(layered()).toEqual([true, true, true]);
		expect(item().style.transform).toBe('translate3d(-144px, 0, 0)');
	});

	it('keeps them until the close animation ends, then drops them', () => {
		setProps({ swiping: true });
		reveal();
		close();
		expect(layered()).toEqual([true, true, true]);
		expect(item().style.transform).toBe('translate3d(0px, 0, 0)');
		endSlide('background-color');
		expect(layered()).toEqual([true, true, true]);
		endSlide();
		expect(layered()).toEqual([false, false, false]);
		expect(wrapper().classList.contains('engaged')).toBe(false);
	});

	it('drops them after a short wait when no transition runs (a tap that never moved)', () => {
		setProps({ swiping: true });
		setProps({ swiping: false });
		expect(layered()).toEqual([true, true, true]);
		vi.advanceTimersByTime(299);
		flushSync();
		expect(layered()).toEqual([true, true, true]);
		vi.advanceTimersByTime(2);
		flushSync();
		expect(layered()).toEqual([false, false, false]);
	});

	it('keeps them when a new swipe interrupts the close, and again after that swipe closes', () => {
		setProps({ swiping: true });
		reveal();
		close();
		vi.advanceTimersByTime(100);
		setProps({ swiping: true, isSwipeGesture: true, transformX: -40 });
		vi.advanceTimersByTime(1000);
		flushSync();
		expect(layered()).toEqual([true, true, true]);
		expect(item().style.transform).toBe('translate3d(-40px, 0, 0)');
		close();
		expect(layered()).toEqual([true, true, true]);
		endSlide();
		expect(layered()).toEqual([false, false, false]);
	});
});
