import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import ThreadPausedMark from './ThreadPausedMark.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('ThreadPausedMark', () => {
	it('renders the gray pause bars with one accessible name', () => {
		component = mount(ThreadPausedMark, { target: host });
		flushSync();

		const mark = host.querySelector<SVGElement>('svg.paused-mark');
		expect(mark).not.toBeNull();
		expect(mark?.getAttribute('role')).toBe('img');
		expect(mark?.getAttribute('aria-label')).toBe('Paused');
		expect(mark?.querySelector('title')?.textContent).toBe('Paused');
		// Two bars: the pause glyph, never a dot that reads as unread.
		expect(mark?.querySelectorAll('rect')).toHaveLength(2);
	});

	it('scales to the row it leads without losing the token gray', () => {
		component = mount(ThreadPausedMark, { target: host, props: { size: 12 } });
		flushSync();

		const mark = host.querySelector<SVGElement>('svg.paused-mark');
		expect(mark?.getAttribute('width')).toBe('12');
		expect(mark?.getAttribute('height')).toBe('12');
		const bars = mark?.querySelectorAll<SVGRectElement>('rect');
		expect(bars?.[0].getAttribute('fill')).toBe('var(--text-tertiary)');
		expect(bars?.[1].getAttribute('fill')).toBe('var(--text-tertiary)');
	});
});
