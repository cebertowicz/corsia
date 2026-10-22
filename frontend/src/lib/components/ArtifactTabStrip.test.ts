import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ArtifactTabStrip from './ArtifactTabStrip.svelte';
import type { ArtifactTab } from '$lib/stores/artifactWorkspace';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const tabs: ArtifactTab[] = [
	{ kind: 'document', instanceId: 'tab-1', artifactId: 'art-1', path: '/a.md', title: 'a.md', threadId: null, loadedRevision: 'r1', latestRevision: 'r1', status: 'ready' },
	{ kind: 'document', instanceId: 'tab-2', artifactId: 'art-2', path: '/b.md', title: 'b.md', threadId: null, loadedRevision: null, latestRevision: null, status: 'loading' },
	{ kind: 'document', instanceId: 'tab-3', artifactId: 'art-3', path: '/c.md', title: 'c.md', threadId: null, loadedRevision: null, latestRevision: null, status: 'error' },
];

/** Same document, loaded at r1 while r2 exists on the server. */
const changedTab: ArtifactTab = {
	kind: 'document', instanceId: 'tab-4', artifactId: 'art-4', path: '/d.md', title: 'd.md', threadId: null,
	loadedRevision: 'r1', latestRevision: 'r2', status: 'ready',
};

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

function renderStrip(overrides: Partial<{
	activeKey: string | null;
	onSelect: (key: string) => void;
	onClose: (key: string) => void;
	onCollapse: () => void;
}> = {}) {
	component = mount(ArtifactTabStrip, {
		target: host,
		props: {
			tabs,
			activeKey: overrides.activeKey ?? 'tab-1',
			onSelect: overrides.onSelect ?? (() => {}),
			onClose: overrides.onClose ?? (() => {}),
			onCollapse: overrides.onCollapse ?? (() => {}),
		},
	});
	flushSync();
}

describe('ArtifactTabStrip', () => {
	it('renders every tab title and marks the active one selected', () => {
		renderStrip({ activeKey: 'tab-2' });
		const tabEls = host.querySelectorAll('[role="tab"]');
		expect(tabEls).toHaveLength(3);
		expect(host.textContent).toContain('a.md');
		expect(host.textContent).toContain('b.md');
		expect(host.textContent).toContain('c.md');
		expect(tabEls[1].getAttribute('aria-selected')).toBe('true');
		expect(tabEls[0].getAttribute('aria-selected')).toBe('false');
	});

	it('shows an error indicator only for the failed tab', () => {
		renderStrip();
		const errored = Array.from(host.querySelectorAll('.tab')).find((el) => el.textContent?.includes('c.md'));
		expect(errored?.querySelector('.tab-err')).not.toBeNull();
		const ok = Array.from(host.querySelectorAll('.tab')).find((el) => el.textContent?.includes('a.md'));
		expect(ok?.querySelector('.tab-err')).toBeNull();
	});

	it('clicking a tab calls onSelect with its key', () => {
		const onSelect = vi.fn();
		renderStrip({ onSelect });
		const tabEls = host.querySelectorAll<HTMLElement>('[role="tab"]');
		tabEls[2].click();
		expect(onSelect).toHaveBeenCalledWith('tab-3');
	});

	it('clicking the close button calls onClose without also selecting the tab', () => {
		const onSelect = vi.fn();
		const onClose = vi.fn();
		renderStrip({ onSelect, onClose });
		const closeButtons = host.querySelectorAll<HTMLElement>('.tab-close');
		closeButtons[1].click();
		expect(onClose).toHaveBeenCalledWith('tab-2');
		expect(onSelect).not.toHaveBeenCalled();
	});

	it('keeps tab selection and close as sibling controls instead of nested buttons', () => {
		renderStrip();
		const wrap = host.querySelector('.tab-wrap');
		expect(wrap?.querySelector(':scope > [role="tab"]')).not.toBeNull();
		expect(wrap?.querySelector(':scope > .tab-close')).not.toBeNull();
		expect(wrap?.querySelector('[role="tab"] .tab-close')).toBeNull();
	});

	it('ArrowRight/ArrowLeft on a focused tab moves selection to the neighbor (wrapping)', () => {
		const onSelect = vi.fn();
		renderStrip({ activeKey: 'tab-1', onSelect });
		const tabEls = host.querySelectorAll<HTMLElement>('[role="tab"]');
		tabEls[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
		expect(onSelect).toHaveBeenCalledWith('tab-2');

		onSelect.mockClear();
		tabEls[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
		expect(onSelect).toHaveBeenCalledWith('tab-3'); // wraps to the last tab
	});

	it('scrolls the active and keyboard-focused tab into view', async () => {
		const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
		const scrollIntoView = vi.fn();
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView });
		try {
			renderStrip({ activeKey: 'tab-3' });
			await tick();
			expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });

			scrollIntoView.mockClear();
			host.querySelectorAll<HTMLElement>('[role="tab"]')[1].focus();
			expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
		} finally {
			if (original) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', original);
			else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
		}
	});

	it('the collapse button calls onCollapse', () => {
		const onCollapse = vi.fn();
		renderStrip({ onCollapse });
		host.querySelector<HTMLElement>('.collapse-btn')?.click();
		expect(onCollapse).toHaveBeenCalledOnce();
	});
});

describe('ArtifactTabStrip — changed indicator', () => {
	it('dots a tab whose document changed since it loaded, and only that tab', () => {
		component = mount(ArtifactTabStrip, {
			target: host,
			props: {
				tabs: [...tabs, changedTab],
				activeKey: 'tab-1',
				onSelect: () => {},
				onClose: () => {},
				onCollapse: () => {},
			},
		});
		flushSync();
		const dots = host.querySelectorAll('.tab-dot');
		expect(dots).toHaveLength(1);
		expect(dots[0].closest('.tab-wrap')?.textContent).toContain('d.md');
	});
});
