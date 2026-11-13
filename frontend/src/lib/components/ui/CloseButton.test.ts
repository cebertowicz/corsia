import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CloseButton from './CloseButton.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });

describe('CloseButton', () => {
	it('is a button named "Close" that calls onclick', () => {
		host = document.createElement('div'); document.body.appendChild(host);
		const onclick = vi.fn();
		component = mount(CloseButton, { target: host, props: { onclick } });
		flushSync();
		const button = host.querySelector<HTMLButtonElement>('button')!;
		expect(button.getAttribute('aria-label')).toBe('Close');
		expect(button.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
		button.click();
		expect(onclick).toHaveBeenCalledOnce();
	});
});
