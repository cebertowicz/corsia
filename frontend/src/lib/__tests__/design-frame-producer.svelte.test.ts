/**
 * The producer half of PLANS-AC34's `?frame=attention-dock` mode.
 *
 * `AttentionDockShowcase` already emits `<iframe src="/_design?frame=…">` and
 * renders a bare dock when it reads that query — but the route rendered every
 * showcase unconditionally, so the iframe loaded the whole design index. A
 * container width cannot fire a viewport media query, so without a real frame
 * the mobile modes prove nothing.
 *
 * Red at `ac1f9f8d`: the framed route renders the entire design system.
 */
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DesignPage from '../../routes/_design/+page.svelte';
import { __reset } from '$lib/stores/attentionDock.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

async function settle() {
	for (let i = 0; i < 12; i += 1) {
		await Promise.resolve();
		await tick();
		flushSync();
	}
}

function setSearch(search: string) {
	// jsdom allows replaceState onto the same origin; the route reads the URL
	// once because a frame is loaded fresh and never client-navigated.
	window.history.replaceState({}, '', `/_design${search}`);
}

function headings(): string[] {
	return Array.from(host.querySelectorAll('h1, h2')).map(
		(node) => (node.textContent ?? '').trim(),
	);
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	__reset();
	vi.unstubAllGlobals();
	vi.stubGlobal('Response', Response);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	setSearch('');
	__reset();
	vi.restoreAllMocks();
});

describe('/_design?frame=attention-dock producer', () => {
	it('renders only the attention-dock showcase when framed', async () => {
		setSearch('?frame=attention-dock');
		component = mount(DesignPage, { target: host });
		await settle();

		const seen = headings();
		expect(seen).not.toContain('Design system');
		expect(host.querySelector('[data-attention-dock]')).toBeTruthy();
	});

	it('does not nest another iframe inside the frame', async () => {
		// The showcase's own mobile modes must not recurse: the framed mode is
		// the dock alone, with no viewport switcher of its own.
		setSearch('?frame=attention-dock');
		component = mount(DesignPage, { target: host });
		await settle();

		expect(host.querySelectorAll('iframe').length).toBe(0);
	});

	it('renders exactly one dock in the frame', async () => {
		setSearch('?frame=attention-dock&scenario=one');
		component = mount(DesignPage, { target: host });
		await settle();

		expect(host.querySelectorAll('[data-attention-dock]').length).toBe(1);
	});

	it('honours the scenario the frame was asked for', async () => {
		setSearch('?frame=attention-dock&scenario=empty');
		component = mount(DesignPage, { target: host });
		await settle();

		// An empty queue renders nothing at all, framed or not.
		expect(host.querySelectorAll('[data-attention-dock]').length).toBe(0);
	});

	it('still renders the full design system when not framed', () => {
		setSearch('');
		component = mount(DesignPage, { target: host });
		// This assertion needs the synchronously mounted route, not the dock
		// showcase's asynchronous queue lifecycle that `settle` drains.
		flushSync();

		expect(headings()).toContain('Design system');
	});

	it('still offers the dock showcase inside the unframed page', async () => {
		setSearch('');
		component = mount(DesignPage, { target: host });
		await settle();

		expect(headings()).toContain('Attention dock — the one thing that needs you');
	});
});
