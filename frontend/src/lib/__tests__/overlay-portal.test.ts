import { readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import Modal from '$lib/components/ui/Modal.svelte';
import Popover from '$lib/components/ui/Popover.svelte';

/**
 * A `position: fixed` layer mounted inside a pane is clipped to that pane, or painted under its
 * neighbours, in a WebKit webview: the chat column cut a dialog off at the open document, and the
 * document cut off a confirmation the other way round. The fix is never per dialog. Every overlay
 * mounts through `use:portal`, which puts it on `document.body`, and the shared primitives
 * (Modal, Popover, Menu, AdaptiveDrawer) do it for their callers.
 *
 * This guard reads the authored sources, so it fails on the line a contributor wrote.
 */

const SRC = resolve(__dirname, '../..');

/**
 * Fixed layers that are not overlays above the panes: shell chrome, or a control drawn inside the
 * surface it belongs to. Anything else that is `position: fixed` must use `use:portal`.
 */
const NOT_AN_OVERLAY: Record<string, string> = {
	'lib/components/HomeShell.svelte': 'the app root every pane sits inside',
	'lib/components/Header.svelte': 'the header band; its overflow sheet hangs from it above the panes',
	'lib/components/PaneShell.svelte': 'the right pane itself on a phone',
	'lib/components/InputBar.css': 'the composer floater on a phone',
	'lib/components/NewChatFab.svelte': 'a floating button and its tap-away layer, mounted at the app root beside the rail',
	'lib/components/SidebarContextMenu.svelte': 'mounted at the app root beside the rail; ui/Menu replaces it (#857)',
	'lib/components/ToastContainer.svelte': 'mounted at the app root and anchored to the main column on purpose',
	'lib/components/VersionOverlay.svelte': 'a status chip mounted at the app root',
	'lib/components/Lightbox.svelte': 'mounted by HomeOverlays beside the app root, above every pane',
	'lib/components/CommandPalette.svelte': 'mounted by HomeOverlays beside the app root, above every pane',
	'lib/components/ReviewBarShell.svelte': 'the review capsule, drawn inside the document it reviews',
	'lib/components/DocViewer.css': 'the selection chip, drawn inside the document it comments on',
};

function sources(dir: string): string[] {
	return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) {
			return entry.name === '__tests__' || entry.name === '_design' ? [] : sources(path);
		}
		return /\.(svelte|css)$/.test(entry.name) && !/\.test\./.test(entry.name) ? [path] : [];
	});
}

const fixedLayers = [...sources(join(SRC, 'lib')), ...sources(join(SRC, 'routes'))]
	.map((path) => ({ path: relative(SRC, path), text: readFileSync(path, 'utf8') }))
	.filter(({ text }) => /position:\s*fixed/.test(text));

describe('overlays mount on document.body', () => {
	it('portals every fixed layer that is not shell chrome or part of its own surface', () => {
		const unportaled = fixedLayers
			.filter(({ path, text }) => !text.includes('use:portal') && !(path in NOT_AN_OVERLAY))
			.map(({ path }) => path);
		expect(
			unportaled,
			'A fixed layer inside a pane is clipped to it in WebKit. Add use:portal (see ui/Modal.svelte), or list the file in NOT_AN_OVERLAY with the reason it is not an overlay.',
		).toEqual([]);
	});

	it('keeps NOT_AN_OVERLAY honest: each entry still has a fixed layer and still lacks a portal', () => {
		const stale = Object.keys(NOT_AN_OVERLAY).filter((path) => {
			const found = fixedLayers.find((layer) => layer.path === path);
			return !found || found.text.includes('use:portal');
		});
		expect(stale).toEqual([]);
	});
});

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

/** A pane that clips and stacks like the chat column: overflow hidden around the opener. */
function pane(): HTMLElement {
	host = document.createElement('div');
	host.style.cssText = 'overflow: hidden; position: relative; z-index: 0;';
	document.body.appendChild(host);
	return host;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	document.body.innerHTML = '';
});

describe('the overlay primitives leave the pane they were opened in', () => {
	it('Modal renders a portaled layer with sibling scrim and card', async () => {
		component = mount(Modal, { target: pane(), props: { title: 'Add model', onClose: vi.fn(), children: (() => {}) as never } });
		flushSync();
		await tick();
		const layer = document.querySelector<HTMLElement>('.modal-layer');
		const scrim = document.querySelector<HTMLElement>('.bg');
		const card = document.querySelector<HTMLElement>('[role="dialog"]');
		expect(layer).not.toBeNull();
		expect(layer!.parentElement).toBe(document.body);
		expect(scrim?.parentElement).toBe(layer);
		expect(card?.parentElement).toBe(layer);
		expect(host.contains(layer)).toBe(false);
	});

	it('Popover renders its card directly under document.body', async () => {
		component = mount(Popover, {
			target: pane(),
			props: { anchor: new DOMRect(8, 8, 10, 10), open: true, onClose: vi.fn(), children: (() => {}) as never },
		});
		flushSync();
		await tick();
		const card = document.querySelector<HTMLElement>('[data-popover]');
		expect(card).not.toBeNull();
		expect(card!.parentElement).toBe(document.body);
		expect(host.contains(card)).toBe(false);
	});

	it('removes the card from the body when its owner goes away', async () => {
		component = mount(Popover, {
			target: pane(),
			props: { anchor: new DOMRect(8, 8, 10, 10), open: true, onClose: vi.fn(), children: (() => {}) as never },
		});
		flushSync();
		await tick();
		unmount(component);
		component = null;
		expect(document.querySelector('[data-popover]')).toBeNull();
	});
});
