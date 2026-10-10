import { get } from 'svelte/store';
import { mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { toasts } from '$lib/stores/toast';
import ToastContainer from './ToastContainer.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function clearToasts() {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
}

beforeEach(() => {
	vi.useFakeTimers();
	clearToasts();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	clearToasts();
	host.remove();
	vi.useRealTimers();
});

function showActionToast() {
	(toasts as any).showAction('Removed from pane', { label: 'Undo', run: vi.fn() });
	component = mount(ToastContainer, { target: host });
}

describe('ToastContainer action', () => {
	it('renders the visible action label on a real button', () => {
		showActionToast();
		const action = [...host.querySelectorAll('button')].find(
			(button) => button.textContent?.trim() === 'Undo',
		);
		expect(action).toBeTruthy();
		expect(action?.getAttribute('aria-label') ?? action?.textContent?.trim()).toBe('Undo');
	});

	it('pauses auto-dismiss while the pointer is inside', async () => {
		showActionToast();
		const toast = host.querySelector<HTMLElement>('.toast')!;
		await vi.advanceTimersByTimeAsync(7000);
		toast.dispatchEvent(new MouseEvent('mouseenter'));
		await vi.advanceTimersByTimeAsync(2000);
		expect(get(toasts)).toHaveLength(1);
		toast.dispatchEvent(new MouseEvent('mouseleave'));
		await vi.advanceTimersByTimeAsync(1001);
		expect(get(toasts)).toHaveLength(0);
	});

	it('stays paused until both pointer and focus leave', async () => {
		showActionToast();
		const toast = host.querySelector<HTMLElement>('.toast')!;
		const action = [...host.querySelectorAll<HTMLButtonElement>('button')].find(
			(button) => button.textContent?.trim() === 'Undo',
		)!;
		await vi.advanceTimersByTimeAsync(7000);
		toast.dispatchEvent(new MouseEvent('mouseenter'));
		action.focus();
		toast.dispatchEvent(new MouseEvent('mouseleave'));
		await vi.advanceTimersByTimeAsync(2000);
		expect(get(toasts)).toHaveLength(1);
		action.blur();
		await vi.advanceTimersByTimeAsync(1001);
		expect(get(toasts)).toHaveLength(0);
	});

	it('pauses auto-dismiss while keyboard focus is inside', async () => {
		showActionToast();
		const action = [...host.querySelectorAll<HTMLButtonElement>('button')].find(
			(button) => button.textContent?.trim() === 'Undo',
		)!;
		await vi.advanceTimersByTimeAsync(7000);
		action.focus();
		await vi.advanceTimersByTimeAsync(2000);
		expect(get(toasts)).toHaveLength(1);
		action.blur();
		await vi.advanceTimersByTimeAsync(1001);
		expect(get(toasts)).toHaveLength(0);
	});
});
