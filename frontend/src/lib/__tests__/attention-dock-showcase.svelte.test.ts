/**
 * The PLANS-AC34 `/_design` entry, verified rather than merely written.
 *
 * The showcase drives the production `AttentionDock` and the production store,
 * stubbing only the network — so if these scenarios render, the real component
 * renders them. The real-browser pass over the same entry is written in
 * `frontend/e2e/attention-dock.spec.ts` and is currently blocked by a host
 * Chromium crash (see the thread's BROWSER-HARNESS-FAILURE.md).
 */
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AttentionDockShowcase from '../../routes/_design/AttentionDockShowcase.svelte';
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

function scenario(label: string): HTMLButtonElement {
	const match = Array.from(host.querySelectorAll('button')).find(
		(button) => (button.textContent ?? '').trim() === label,
	);
	expect(match, label).toBeTruthy();
	return match as HTMLButtonElement;
}

async function choose(label: string) {
	scenario(label).click();
	await settle();
}

function docks(): NodeListOf<Element> {
	return host.querySelectorAll('[data-attention-dock]');
}

beforeEach(() => {
	// The drawer portals to document.body, so the host is the body itself.
	host = document.body;
	__reset();
	vi.unstubAllGlobals();
	// The showcase installs its own fetch shim on mount.
	vi.stubGlobal('Response', Response);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.replaceChildren();
	__reset();
	vi.restoreAllMocks();
});

describe('/_design attention dock showcase (PLANS-AC34)', () => {
	it('offers every state the acceptance criterion names', async () => {
		component = mount(AttentionDockShowcase, { target: host });
		await settle();
		for (const label of [
			'0 waiting', '1 waiting', 'Many waiting', 'No parent review',
			'External review', 'Review, then decisions', 'Host lease holds the slot',
			'Stale / offline',
		]) {
			expect(scenario(label), label).toBeTruthy();
		}
	});

	it('renders exactly one dock, never a fabricated side-by-side composition', async () => {
		component = mount(AttentionDockShowcase, { target: host });
		await settle();
		// The real shell makes its mount points mutually exclusive; the showcase
		// must not imply two can be live at once.
		expect(docks().length).toBe(1);

		await choose('0 waiting');
		expect(docks().length).toBe(0);
	});

	it('previews the mobile widths as real viewports, not container widths', async () => {
		// A container width cannot fire `@media (max-width: 620px)`. Each mobile
		// mode loads one dock in an iframe, which has its own viewport — so the
		// media query is the browser's answer, and one dock stays one dock.
		component = mount(AttentionDockShowcase, { target: host });
		await settle();

		for (const [label, width] of [['390', '390px'], ['320', '320px']] as const) {
			await choose(label);
			const frame = host.querySelector('iframe') as HTMLIFrameElement;
			expect(frame, label).toBeTruthy();
			expect(frame.getAttribute('src')).toContain('frame=attention-dock');
			expect(frame.style.width).toBe(width);
			// The parent renders no dock of its own while a frame owns it.
			expect(docks().length, label).toBe(0);
		}

		await choose('Desktop');
		expect(host.querySelector('iframe')).toBeFalsy();
		expect(docks().length).toBe(1);
	});

	it('shows the count for many and opens the chip explicitly', async () => {
		component = mount(AttentionDockShowcase, { target: host });
		await settle();
		await choose('Many waiting');
		expect(host.textContent).toContain('4 waiting');
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);

		(host.querySelector('.dock-bar') as HTMLButtonElement).click();
		await settle();
		expect(host.querySelectorAll('.decision-chip').length).toBe(1);
	});

	it('labels a root-direct raise truthfully', async () => {
		component = mount(AttentionDockShowcase, { target: host });
		await settle();

		await choose('No parent review');
		(host.querySelector('.dock-bar') as HTMLButtonElement).click();
		await settle();
		expect(host.textContent).toMatch(/raised directly — no parent review/i);
	});

	it('hands an external review off to Documents and never offers to clear it', async () => {
		component = mount(AttentionDockShowcase, { target: host });
		await settle();
		await choose('External review');
		(host.querySelector('.dock-bar') as HTMLButtonElement).click();
		await settle();

		expect(host.textContent).toContain('Open in documents');
		const labels = Array.from(host.querySelectorAll('button')).map((b) =>
			(b.textContent ?? '').toLowerCase(),
		);
		expect(labels.some((l) => l.includes('clear'))).toBe(false);
		expect(labels.some((l) => l.includes('dismiss'))).toBe(false);
	});

	it('defers to a host lease holding the slot', async () => {
		component = mount(AttentionDockShowcase, { target: host });
		await settle();
		await choose('Host lease holds the slot');

		const bar = host.querySelector('.dock-bar') as HTMLButtonElement;
		// Deferred, not removed from the tab order: it still says why.
		expect(bar.getAttribute('aria-disabled')).toBe('true');
		expect(bar.hasAttribute('disabled')).toBe(false);
		bar.click();
		await settle();
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
	});

	it('keeps last-known data and says so when the queue read fails', async () => {
		component = mount(AttentionDockShowcase, { target: host });
		await settle();
		await choose('Stale / offline');
		expect(host.textContent).toContain('Showing last known items.');
		// Last-known data is retained, not blanked.
		expect(docks().length).toBeGreaterThan(0);
	});
});
