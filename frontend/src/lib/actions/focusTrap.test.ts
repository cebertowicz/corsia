import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { focusTrap } from './focusTrap';

let outside: HTMLButtonElement;
let node: HTMLElement;

function key(k: string, shiftKey = false) {
	const event = new KeyboardEvent('keydown', { key: k, shiftKey, bubbles: true });
	vi.spyOn(event, 'preventDefault');
	node.dispatchEvent(event);
	return event;
}

beforeEach(() => {
	document.body.innerHTML = '';
	outside = document.createElement('button');
	outside.textContent = 'outside';
	document.body.appendChild(outside);
	outside.focus();

	node = document.createElement('div');
	node.tabIndex = -1;
	node.innerHTML = '<button id="first">a</button><button id="mid">b</button><button id="last">c</button>';
	document.body.appendChild(node);
});

afterEach(() => {
	vi.restoreAllMocks();
	document.body.innerHTML = '';
});

describe('focusTrap', () => {
	it('moves focus into the trap on mount', async () => {
		const trap = focusTrap(node);
		await tick();
		expect(document.activeElement?.id).toBe('first');
		trap.destroy();
	});

	it('wraps Tab from the last element back to the first', async () => {
		const trap = focusTrap(node);
		await tick();
		node.querySelector<HTMLElement>('#last')!.focus();
		expect(key('Tab').preventDefault).toHaveBeenCalledOnce();
		expect(document.activeElement?.id).toBe('first');
		trap.destroy();
	});

	it('wraps Shift+Tab from the first element back to the last', async () => {
		const trap = focusTrap(node);
		await tick();
		node.querySelector<HTMLElement>('#first')!.focus();
		expect(key('Tab', true).preventDefault).toHaveBeenCalledOnce();
		expect(document.activeElement?.id).toBe('last');
		trap.destroy();
	});

	it('leaves Tab alone in the middle of the trap', async () => {
		const trap = focusTrap(node);
		await tick();
		node.querySelector<HTMLElement>('#mid')!.focus();
		expect(key('Tab').preventDefault).not.toHaveBeenCalled();
		trap.destroy();
	});

	it('reports Escape to the caller instead of closing anything itself', async () => {
		const onEscape = vi.fn();
		const trap = focusTrap(node, { onEscape });
		await tick();
		key('Escape');
		expect(onEscape).toHaveBeenCalledOnce();
		trap.destroy();
	});

	it('restores the previously focused element on destroy', async () => {
		const trap = focusTrap(node);
		await tick();
		expect(document.activeElement?.id).toBe('first');
		trap.destroy();
		expect(document.activeElement).toBe(outside);
	});

	it('never calls focus on <body> when nothing was focused at mount', async () => {
		// The trigger can already be unmounted when the trap mounts, leaving <body> as the
		// previous element. In a real browser body.focus() blurs the control that holds focus.
		(document.activeElement as HTMLElement).blur();
		expect(document.activeElement).toBe(document.body);
		const bodyFocus = vi.spyOn(document.body, 'focus');
		const trap = focusTrap(node);
		await tick();
		trap.destroy();
		expect(bodyFocus).not.toHaveBeenCalled();
	});

	it('leaves focus alone when a control outside the trap already took it', async () => {
		const trap = focusTrap(node);
		await tick();
		const bar = document.createElement('button');
		document.body.appendChild(bar);
		bar.focus();
		trap.destroy();
		expect(document.activeElement).toBe(bar);
	});
});
