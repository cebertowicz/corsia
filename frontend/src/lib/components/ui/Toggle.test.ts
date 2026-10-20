import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Toggle from './Toggle.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(props: Record<string, unknown>) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(Toggle, { target: host, props });
	flushSync();
	return host.querySelector<HTMLButtonElement>('button')!;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

describe('Toggle held state', () => {
	it('is aria-disabled, focusable, muted by its styling hook, and ignores clicks while preserving its description', () => {
		const onchange = vi.fn();
		const toggle = render({ checked: true, held: true, onchange, ariaLabel: 'Local only', describedBy: 'local-only-description' });

		expect(toggle.disabled).toBe(false);
		expect(toggle.getAttribute('aria-disabled')).toBe('true');
		expect(toggle.getAttribute('aria-describedby')).toBe('local-only-description');
		expect(toggle.classList.contains('held')).toBe(true);
		toggle.focus();
		expect(document.activeElement).toBe(toggle);
		toggle.click();
		expect(onchange).not.toHaveBeenCalled();
	});
});
