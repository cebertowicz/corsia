import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ArtifactTabSheet from './ArtifactTabSheet.svelte';
import type { ArtifactTab } from '$lib/stores/artifactWorkspace';
import { resetBackLayers, backLayerDepth } from '$lib/historyBackLayer';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const tabs: ArtifactTab[] = [
	{ kind: 'document', instanceId: 'tab-1', artifactId: 'art-1', path: '/a.md', title: 'alpha.md', threadId: null, loadedRevision: 'r1', latestRevision: 'r1', status: 'ready' },
	{ kind: 'document', instanceId: 'tab-2', artifactId: 'art-2', path: '/b.md', title: 'beta.md', threadId: null, loadedRevision: 'r1', latestRevision: 'r2', status: 'ready' },
	{ kind: 'document', instanceId: 'tab-3', artifactId: 'art-3', path: '/c.md', title: 'gamma.md', threadId: null, loadedRevision: null, latestRevision: null, status: 'unavailable' },
	{ kind: 'document', instanceId: 'tab-4', artifactId: 'art-4', path: '/d.md', title: 'delta.md', threadId: null, loadedRevision: 'r1', latestRevision: 'r1', status: 'readonly' },
];

function render(overrides: Partial<{
	activeKey: string | null;
	onSelect: (id: string) => void;
	onClose: (id: string) => void;
	onCloseSheet: () => void;
}> = {}) {
	component = mount(ArtifactTabSheet, {
		target: host,
		props: {
			tabs,
			activeKey: overrides.activeKey ?? 'tab-1',
			onSelect: overrides.onSelect ?? (() => {}),
			onClose: overrides.onClose ?? (() => {}),
			onCloseSheet: overrides.onCloseSheet ?? (() => {}),
		},
	});
	flushSync();
}

beforeEach(() => {
	resetBackLayers();
	vi.spyOn(history, 'pushState').mockImplementation(() => {});
	vi.spyOn(history, 'back').mockImplementation(() => {});
	// The drawer portals to document.body, so the host is the body itself.
	host = document.body;
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.replaceChildren();
	resetBackLayers();
	vi.restoreAllMocks();
});

describe('ArtifactTabSheet', () => {
	it('lists every tab with its full title and marks the active one', () => {
		render();
		const rows = host.querySelectorAll('.sheet-row');
		expect(rows).toHaveLength(4);
		expect(host.textContent).toContain('alpha.md');
		expect(host.textContent).toContain('gamma.md');
		expect(host.querySelector('.sheet-row.active')?.textContent).toContain('alpha.md');
	});

	it('dots only the tab whose document changed since it loaded', () => {
		render();
		const dots = host.querySelectorAll('.sheet-dot');
		expect(dots).toHaveLength(1);
		expect(dots[0].closest('.sheet-row')?.textContent).toContain('beta.md');
	});

	it('labels unavailable and read-only tabs instead of dotting them', () => {
		render();
		const tags = Array.from(host.querySelectorAll('.sheet-tag')).map((el) => el.textContent);
		expect(tags).toEqual(['Unavailable', 'Read-only']);
	});

	it('is an accessible modal dialog', () => {
		render();
		const dialog = host.querySelector('[role="dialog"]');
		expect(dialog?.getAttribute('aria-modal')).toBe('true');
		expect(dialog?.getAttribute('aria-label')).toBe('Open side panel items');
	});

	it('moves focus inside the sheet on open', async () => {
		render();
		await tick();
		expect(host.querySelector('[role="dialog"]')?.contains(document.activeElement)).toBe(true);
	});

	it('selecting a tab switches to it and closes the sheet', () => {
		const onSelect = vi.fn();
		const onCloseSheet = vi.fn();
		render({ onSelect, onCloseSheet });
		host.querySelectorAll<HTMLButtonElement>('.sheet-pick')[1].click();
		expect(onSelect).toHaveBeenCalledWith('tab-2');
		expect(onCloseSheet).toHaveBeenCalledOnce();
	});

	it('closing a tab does not also select it', () => {
		const onClose = vi.fn();
		const onSelect = vi.fn();
		render({ onClose, onSelect });
		host.querySelectorAll<HTMLButtonElement>('.sheet-close')[2].click();
		expect(onClose).toHaveBeenCalledWith('tab-3');
		expect(onSelect).not.toHaveBeenCalled();
	});

	it('keeps select and close as siblings rather than nested interactive controls', () => {
		render();
		expect(host.querySelector('button button')).toBeNull();
	});

	it('Escape closes only the sheet and consumes the event', () => {
		const onCloseSheet = vi.fn();
		const parentEscape = vi.fn();
		host.addEventListener('keydown', parentEscape);
		render({ onCloseSheet });
		const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		host.querySelector('[role="dialog"]')!.dispatchEvent(event);
		expect(onCloseSheet).toHaveBeenCalledOnce();
		expect(parentEscape).not.toHaveBeenCalled();
		expect(event.defaultPrevented).toBe(true);
		host.removeEventListener('keydown', parentEscape);
	});

	it('clicking the backdrop closes the sheet, clicking the sheet does not', () => {
		const onCloseSheet = vi.fn();
		render({ onCloseSheet });
		host.querySelector<HTMLElement>('.drawer-panel')!.click();
		expect(onCloseSheet).not.toHaveBeenCalled();
		host.querySelector<HTMLElement>('.drawer-backdrop')!.click();
		expect(onCloseSheet).toHaveBeenCalledOnce();
	});

	/**
	 * The sheet is presented by AdaptiveDrawer rather than duplicating its own
	 * backdrop, transitions, focus trap, back layer and geometry. The duplicate is
	 * what drifted into a transparent background.
	 */
	it('is presented by the shared drawer, not a second sheet implementation', () => {
		render();
		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(1);
		expect(host.querySelector('.sheet-backdrop')).toBeNull();
		expect(host.querySelector('.sheet')).toBeNull();
	});

	it('titles the drawer header and dismisses from its Done control', () => {
		const onCloseSheet = vi.fn();
		render({ onCloseSheet });
		expect(host.querySelector('.drawer-head h2')?.textContent).toBe('Open items');
		const done = host.querySelector<HTMLButtonElement>('.drawer-done')!;
		// Visible text is also the accessible name.
		expect(done.textContent?.trim()).toBe('Done');
		expect(done.getAttribute('aria-label')).toBeNull();
		done.click();
		expect(onCloseSheet).toHaveBeenCalledOnce();
	});

	it('opens focused on the active item rather than the dismiss control', async () => {
		render({ activeKey: 'tab-2' });
		await tick();
		expect(document.activeElement?.textContent).toContain('beta.md');
	});

	it('registers a back layer while open so hardware Back closes it first', () => {
		render();
		expect(backLayerDepth()).toBe(1);
		expect(history.pushState).toHaveBeenCalledOnce();
	});

	it('disposes its back layer when the sheet unmounts', () => {
		render();
		expect(backLayerDepth()).toBe(1);
		unmount(component!);
		component = null;
		expect(backLayerDepth()).toBe(0);
		expect(history.back).toHaveBeenCalledOnce();
	});
});
