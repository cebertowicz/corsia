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

describe('ArtifactPane — persistence marker', () => {
	it('marks a pinned row and offers to unpin it', () => {
		mountPane({ nodes: [docNode({ pinned: true })], needs_attention: [] });

		expect(host.querySelector('.pin-mark')).toBeTruthy();
		expect(host.querySelector('button[aria-label="Unpin from pane"]')).toBeTruthy();
		expect(host.querySelector('button[aria-label="Pin to pane"]')).toBeNull();
	});

	it('leaves an unpinned row unmarked but offers to pin it', () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		expect(host.querySelector('.pin-mark')).toBeNull();
		expect(host.querySelector('button[aria-label="Pin to pane"]')).toBeTruthy();
	});

	it('keeps Remove from pane alongside the pin control', () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		expect(host.querySelector('button[aria-label="Remove from pane"]')).toBeTruthy();
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

		const btn = host.querySelector<HTMLButtonElement>('button[aria-label="Pin to pane"]')!;
		expect(btn.title).toBe('Pin — keep this row leading its list');
	});

	it('tells a non-exempt unpinned row pinning keeps it past task end', () => {
		mountPane({ nodes: [docNode({ retirement_exempt: false })], needs_attention: [] });

		const btn = host.querySelector<HTMLButtonElement>('button[aria-label="Pin to pane"]')!;
		expect(btn.title).toBe('Pin — keep this after the task finishes');
	});

	it('offers no pin control on a subscriber-only row', () => {
		mountPane({ nodes: [docNode({ pin_eligible: false })], needs_attention: [] });

		expect(host.querySelector('button[aria-label="Pin to pane"]')).toBeNull();
		expect(host.querySelector('button[aria-label="Unpin from pane"]')).toBeNull();
	});
});

describe('ArtifactPane — pin toggle', () => {
	it('pins a row through the API', async () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		host.querySelector<HTMLButtonElement>('button[aria-label="Pin to pane"]')!.click();

		await vi.waitFor(() => {
			expect(setArtifactPinned).toHaveBeenCalledWith('a1', 'sess-1', true);
		});
	});

	it('unpins a pinned row through the same API', async () => {
		mountPane({ nodes: [docNode({ pinned: true })], needs_attention: [] });

		host.querySelector<HTMLButtonElement>('button[aria-label="Unpin from pane"]')!.click();

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
		host.querySelector<HTMLButtonElement>('button[aria-label="Pin to pane"]')!.click();

		await vi.waitFor(() => {
			expect(setArtifactPinned).toHaveBeenCalledWith('c1', 'sess-1', true);
		});
		expect(host.querySelector('.group-children')).toBeTruthy();
	});
});
