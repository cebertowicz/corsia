import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { pageEscape } from './pageEscape';
import { paneOpen } from '$lib/stores/paneOpen';
import { phoneNavOver } from '$lib/stores/phoneNav.svelte';

const escape = () => new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });

afterEach(() => {
	paneOpen.set(false);
	phoneNavOver.set(false);
});

describe('pageEscape', () => {
	it('leaves an Escape a layer above the page already used', () => {
		paneOpen.set(true);
		const close = vi.fn();
		const event = escape();
		event.preventDefault();
		expect(pageEscape(event, { open: true, close })).toBe(false);
		expect(get(paneOpen)).toBe(true);
		expect(close).not.toHaveBeenCalled();
	});

	it('closes the phone navigation before the pane it slides over', () => {
		paneOpen.set(true);
		phoneNavOver.set(true);
		const close = vi.fn();
		expect(pageEscape(escape(), { open: true, close })).toBe(true);
		expect(close).toHaveBeenCalledTimes(1);
		expect(get(paneOpen)).toBe(true);
	});

	it('closes the pane, then the sidebar, as before', () => {
		paneOpen.set(true);
		const close = vi.fn();
		expect(pageEscape(escape(), { open: true, close })).toBe(true);
		expect(get(paneOpen)).toBe(false);
		expect(close).not.toHaveBeenCalled();
		expect(pageEscape(escape(), { open: true, close })).toBe(true);
		expect(close).toHaveBeenCalledTimes(1);
		expect(pageEscape(escape(), { open: false, close })).toBe(false);
	});

	it('ignores other keys', () => {
		paneOpen.set(true);
		expect(pageEscape(new KeyboardEvent('keydown', { key: 'Enter' }), { open: true, close: vi.fn() })).toBe(false);
		expect(get(paneOpen)).toBe(true);
	});
});
