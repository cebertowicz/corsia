/**
 * `/_design` still renders the full design system when it is not loaded as a frame.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DesignPage from '../../routes/_design/+page.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function setSearch(search: string) {
	window.history.replaceState({}, '', `/_design${search}`);
}

function headings(): string[] {
	return Array.from(host.querySelectorAll('h1, h2')).map((node) => (node.textContent ?? '').trim());
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	vi.unstubAllGlobals();
	vi.stubGlobal('Response', Response);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	setSearch('');
	vi.restoreAllMocks();
});

describe('/_design', () => {
	it('still renders the full design system when not framed', () => {
		setSearch('');
		component = mount(DesignPage, { target: host });
		flushSync();

		expect(headings()).toContain('Design system');
	});
});
