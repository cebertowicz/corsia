/**
 * ArtifactPanePin.test.ts
 *
 * The pane's persistence affordance: which rows read as persistent, and that
 * pinning reaches the API from both a top-level row and a worktree-group child.
 * Split from ArtifactPane.test.ts, which is already over its size budget.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { token, user } from '$lib/stores/auth';
import ArtifactPane from './ArtifactPane.svelte';

vi.mock('$lib/api/docs', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/api/docs')>();
	return {
		...original,
		setArtifactPinned: vi.fn().mockResolvedValue({}),
		setArtifactDisplay: vi.fn().mockResolvedValue({}),
		reconcileArtifactsApply: vi.fn().mockResolvedValue({ ok: true, mode: 'apply', counts: {}, receipts: [] }),
		fetchArtifactPane: vi.fn(),
	};
});

import { setArtifactPinned } from '$lib/api/docs';

function docNode(overrides: Record<string, unknown> = {}) {
	return {
		kind: 'artifact', id: 'a1', integration: 'workspace', type: 'document',
		external_id: 'notes.md', title: null, name: 'notes.md', path: '/workspace/notes.md',
		retention: 'durable', surfacing: 'visible', pinned: false, pin_eligible: true, retirement_exempt: false, state: 'active',
		updated_at: '2026-07-31T12:00:00Z', description: null, metadata: {},
		presentation: { label: 'Document', icon: 'file-text', capability: 'leaf-package' },
		action: { capability: 'leaf-package', disabled: false, disabled_reason: null },
		...overrides,
	};
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('tok');
	user.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
	vi.mocked(setArtifactPinned).mockClear();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	vi.restoreAllMocks();
});

function mountPane(demo: Record<string, unknown>) {
	component = mount(ArtifactPane, {
		target: host,
		props: { threadId: 'sess-1', demo: demo as any },
	});
	flushSync();
}

/** Open the first row's context menu; the menu is portaled to the body. */
function openRowMenu(): void {
	host.querySelector<HTMLElement>('.row-outer')!.dispatchEvent(
		new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 24, clientY: 24 })
	);
	flushSync();
}

function menuItem(label: string): HTMLButtonElement | undefined {
	return [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
		.find((item) => item.textContent?.trim() === label);
}

describe('ArtifactPane — persistence marker', () => {
	it('marks a pinned row once and offers to unpin it from its menu', () => {
		mountPane({ nodes: [docNode({ pinned: true })], needs_attention: [] });

		// One pin mark: the name's, never a second pin control on the row.
		expect(host.querySelectorAll('.pin-mark')).toHaveLength(1);
		expect(host.querySelector('button[aria-label="Unpin from pane"]')).toBeNull();
		openRowMenu();
		expect(menuItem('Unpin from pane')).toBeTruthy();
		expect(menuItem('Pin to pane')).toBeUndefined();
	});

	it('draws no trailing action button and opens its menu from Shift+F10', () => {
		mountPane({ nodes: [docNode({ pinned: true })], needs_attention: [] });
		expect(host.querySelector('button[aria-label^="More actions for"]')).toBeNull();

		host.querySelector<HTMLElement>('.row-outer')!.dispatchEvent(
			new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true, cancelable: true })
		);
		flushSync();
		expect(menuItem('Unpin from pane')).toBeTruthy();
	});

	it('leaves an unpinned row unmarked but offers to pin it', () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		expect(host.querySelector('.pin-mark')).toBeNull();
		openRowMenu();
		expect(menuItem('Pin to pane')).toBeTruthy();
	});

	it('keeps Remove from pane beside Pin in the one row menu', () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		// Secondary actions stay in the menu; the row draws no trailing action control.
		expect(host.querySelectorAll('.row-outer button[aria-label]')).toHaveLength(0);
		openRowMenu();
		expect([...document.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent?.trim()))
			.toEqual(['Pin to pane', 'Remove from pane']);
	});

	it('gives the pin mark exempt-aware wording instead of a fixed claim', () => {
		mountPane({
			nodes: [docNode({ pinned: true, retirement_exempt: true })],
			needs_attention: [],
		});
		const mark = host.querySelector('.pin-mark');
		expect(mark?.getAttribute('title')).toBe('Pinned — stays in the pane and sorts first');
	});

	it('keeps the task-scoped pin-mark wording for a non-exempt pinned row', () => {
		mountPane({
			nodes: [docNode({ pinned: true, retirement_exempt: false })],
			needs_attention: [],
		});
		const mark = host.querySelector('.pin-mark');
		expect(mark?.getAttribute('title')).toBe('Pinned — kept after the task finishes');
	});

	it('tells an exempt unpinned row it only reorders, not persists', () => {
		mountPane({ nodes: [docNode({ retirement_exempt: true })], needs_attention: [] });

		openRowMenu();
		expect(menuItem('Pin to pane')!.title).toBe('Pin — keep this row leading its list');
	});

	it('tells a non-exempt unpinned row pinning keeps it past task end', () => {
		mountPane({ nodes: [docNode({ retirement_exempt: false })], needs_attention: [] });

		openRowMenu();
		expect(menuItem('Pin to pane')!.title).toBe('Pin — keep this after the task finishes');
	});

	it('offers no pin control on a subscriber-only row', () => {
		mountPane({ nodes: [docNode({ pin_eligible: false })], needs_attention: [] });

		openRowMenu();
		expect(menuItem('Pin to pane')).toBeUndefined();
		expect(menuItem('Unpin from pane')).toBeUndefined();
		expect(menuItem('Remove from pane')).toBeTruthy();
	});
});

describe('ArtifactPane — pin toggle', () => {
	it('pins a row through the API', async () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		openRowMenu();
		menuItem('Pin to pane')!.click();

		await vi.waitFor(() => {
			expect(setArtifactPinned).toHaveBeenCalledWith('a1', 'sess-1', true);
		});
	});

	it('unpins a pinned row through the same API', async () => {
		mountPane({ nodes: [docNode({ pinned: true })], needs_attention: [] });

		openRowMenu();
		menuItem('Unpin from pane')!.click();

		await vi.waitFor(() => {
			expect(setArtifactPinned).toHaveBeenCalledWith('a1', 'sess-1', false);
		});
	});

	it('pins a worktree-group child without pulling it out of its group', async () => {
		mountPane({
			nodes: [{
				kind: 'worktree-group', key: '/repo/.worktrees/feat-foo', label: 'feat/foo',
				branch: 'feat/foo', file_count: 1, attention: false,
				children: [docNode({ id: 'c1', name: 'a.py', path: '/repo/.worktrees/feat-foo/a.py' })],
			}],
			needs_attention: [],
		});

		// Disclose the group, then pin the child inside it.
		host.querySelector<HTMLButtonElement>('button[aria-expanded]')!.click();
		flushSync();
		openRowMenu();
		menuItem('Pin to pane')!.click();

		await vi.waitFor(() => {
			expect(setArtifactPinned).toHaveBeenCalledWith('c1', 'sess-1', true);
		});
		expect(host.querySelector('.group-children')).toBeTruthy();
	});
});
