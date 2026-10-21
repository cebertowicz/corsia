/**
 * ArtifactPane.test.ts
 *
 * Coverage for the artifact-pane-hierarchy cutover's client projection:
 * node kinds (artifact / worktree-group), grouping disclosure, capability-driven
 * row actions and disabled states, the needs_attention repair-failure cluster,
 * pane removal, and hide.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { token, user } from '$lib/stores/auth';
import ArtifactPane from './ArtifactPane.svelte';

vi.mock('$lib/stores/artifactWorkspace', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/stores/artifactWorkspace')>();
	return { ...original, openArtifactTab: vi.fn() };
});
vi.mock('$lib/api/docs', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/api/docs')>();
	return {
		...original,
		setArtifactDisplay: vi.fn().mockResolvedValue({}),
		reconcileArtifactsApply: vi.fn().mockResolvedValue({ ok: true, mode: 'apply', counts: {}, receipts: [] }),
		fetchArtifactPane: vi.fn(),
	};
});

import { openArtifactTab } from '$lib/stores/artifactWorkspace';
import { setArtifactDisplay, reconcileArtifactsApply, fetchArtifactPane } from '$lib/api/docs';

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
	vi.mocked(openArtifactTab).mockClear();
	vi.mocked(setArtifactDisplay).mockClear();
	vi.mocked(reconcileArtifactsApply).mockClear();
	vi.mocked(fetchArtifactPane).mockReset();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	user.set(null);
	vi.restoreAllMocks();
});

function mountPane(demo: Record<string, unknown>) {
	component = mount(ArtifactPane, {
		target: host,
		props: { threadId: 'sess-1', demo: demo as any },
	});
	flushSync();
}

describe('ArtifactPane — node kinds', () => {
	it('renders a kind:"artifact" document node as an openable row', () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		const row = [...host.querySelectorAll('button.row.openable')].find((b) => b.textContent?.includes('notes.md'));
		expect(row).toBeTruthy();
	});

	it('opens the artifact tab on click via openArtifactTab (C0 leaf shell unchanged)', () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		const row = [...host.querySelectorAll<HTMLButtonElement>('button.row.openable')].find((b) => b.textContent?.includes('notes.md'))!;
		row.click();

		expect(openArtifactTab).toHaveBeenCalledWith('sess-1', {
			artifactId: 'a1', path: '/workspace/notes.md', title: 'notes.md',
		});
	});

	it('never renders a subagent/session row — no session-derived rows exist in the DTO', () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });
		expect(host.querySelector('.ic.subagent')).toBeNull();
	});
});

describe('ArtifactPane — worktree groups', () => {
	const group = {
		kind: 'worktree-group', key: '/repo/.worktrees/feat-foo', label: 'feat/foo · 2 files',
		branch: 'feat/foo', file_count: 2, attention: false,
		children: [
			docNode({ id: 'c1', name: 'a.py', path: '/repo/.worktrees/feat-foo/a.py' }),
			docNode({ id: 'c2', name: 'b.py', path: '/repo/.worktrees/feat-foo/b.py' }),
		],
	};

	it('renders one collapsed summary row regardless of child count', () => {
		mountPane({ nodes: [group], needs_attention: [] });

		expect(host.querySelector('.group-row')).toBeTruthy();
		expect(host.textContent).toContain('feat/foo · 2 files');
		expect(host.textContent).not.toContain('a.py');
		expect(host.textContent).not.toContain('b.py');
	});

	it('expands to reveal child rows on click, with aria-expanded toggling', () => {
		mountPane({ nodes: [group], needs_attention: [] });

		const summary = host.querySelector<HTMLButtonElement>('.group-row')!;
		expect(summary.getAttribute('aria-expanded')).toBe('false');

		summary.click();
		flushSync();

		expect(summary.getAttribute('aria-expanded')).toBe('true');
		expect(host.textContent).toContain('a.py');
		expect(host.textContent).toContain('b.py');
	});

	it('shows an attention dot only when the group carries attention:true', () => {
		mountPane({
			nodes: [{ ...group, attention: true }],
			needs_attention: [],
		});
		expect(host.querySelector('.dot.attention')).toBeTruthy();
	});
});

describe('ArtifactPane — capability-driven actions', () => {
	it('external-link (pull-request): opens the derived GitHub URL', () => {
		const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
		mountPane({
			nodes: [docNode({
				id: 'pr1', integration: 'github', type: 'pull-request', external_id: 'acme/repo#42',
				name: 'PR #42', path: null,
				presentation: { label: 'Pull Request', icon: 'git-pull-request', capability: 'external-link' },
				action: { capability: 'external-link', disabled: false, disabled_reason: null },
			})],
			needs_attention: [],
		});

		const row = [...host.querySelectorAll<HTMLButtonElement>('button.row.openable')].find((b) => b.textContent?.includes('PR #42'))!;
		row.click();

		expect(openSpy).toHaveBeenCalledWith('https://github.com/acme/repo/pull/42', '_blank', 'noopener,noreferrer');
	});

	it('external-link with an unparseable external_id renders disabled with a visible reason', () => {
		mountPane({
			nodes: [docNode({
				id: 'pr2', integration: 'github', type: 'pull-request', external_id: 'not-a-valid-id',
				name: 'weird PR', path: null,
				presentation: { label: 'Pull Request', icon: 'git-pull-request', capability: 'external-link' },
				action: { capability: 'external-link', disabled: false, disabled_reason: null },
			})],
			needs_attention: [],
		});

		expect(host.querySelector('button.row.openable')).toBeNull();
		expect(host.querySelector('.row.disabled')).toBeTruthy();
		expect(host.textContent).toContain('Could not determine the linked destination.');
	});

	function uploadNode() {
		return docNode({
			id: 'u1', integration: 'upload', type: 'upload', external_id: 'file-1',
			title: 'reference.png', name: 'reference.png', path: '/uploads/steve/file-1/reference.png',
			presentation: { label: 'Upload', icon: 'paperclip', capability: 'download' },
			action: { capability: 'download', disabled: false, disabled_reason: null },
		});
	}

	it('download: disabled with a plain reason when no user id is available', () => {
		mountPane({ nodes: [uploadNode()], needs_attention: [] });
		expect(host.querySelector('button.row.openable')).toBeNull();
		expect(host.textContent).toContain('Sign in again to download this file.');
	});

	it('download: enabled once an authenticated user id is available', () => {
		user.set({ id: 'steve', name: 'Steve' });
		mountPane({ nodes: [uploadNode()], needs_attention: [] });
		const row = [...host.querySelectorAll('button.row.openable')].find((b) => b.textContent?.includes('reference.png'));
		expect(row).toBeTruthy();
	});

	it('unsupported: never clickable, shows the backend-supplied plain reason', () => {
		mountPane({
			nodes: [docNode({
				id: 'x1', integration: 'connector', type: 'mystery', external_id: 'w1',
				name: 'mystery widget', path: null,
				presentation: { label: 'Unsupported', icon: 'file-question', capability: 'unsupported' },
				action: { capability: 'unsupported', disabled: true, disabled_reason: 'No handler is registered for this artifact type.' },
			})],
			needs_attention: [],
		});

		expect(host.querySelector('button.row.openable')).toBeNull();
		const disabledRow = host.querySelector('.row.disabled')!;
		expect(disabledRow).toBeTruthy();
		expect(disabledRow.textContent).toContain('No handler is registered for this artifact type.');
	});

	it('unsupported row still exposes a focusable hide ("Remove from pane") action', () => {
		mountPane({
			nodes: [docNode({
				id: 'x2', integration: 'connector', type: 'mystery', external_id: 'w2',
				name: 'mystery widget 2', path: null,
				presentation: { label: 'Unsupported', icon: 'file-question', capability: 'unsupported' },
				action: { capability: 'unsupported', disabled: true, disabled_reason: 'No handler is registered for this artifact type.' },
			})],
			needs_attention: [],
		});
		expect(removeFromPane()).toBeTruthy();
	});
});

/** The first row's Remove from pane, through its context menu (the menu is portaled to the body). */
function removeFromPane(): HTMLButtonElement | undefined {
	host.querySelector<HTMLElement>('.row-outer')?.dispatchEvent(
		new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 24, clientY: 24 })
	);
	flushSync();
	return [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
		.find((item) => item.textContent?.trim() === 'Remove from pane');
}

describe('ArtifactPane — hide (remove from pane)', () => {
	it('calls setArtifactDisplay(id, threadId, true) when the hide button is clicked', async () => {
		mountPane({ nodes: [docNode()], needs_attention: [] });

		removeFromPane()!.click();

		await vi.waitFor(() => {
			expect(setArtifactDisplay).toHaveBeenCalledWith('a1', 'sess-1', true);
		});
	});
});

describe('ArtifactPane — needs_attention cluster', () => {
	it('renders its repair-failure header', () => {
		mountPane({
			nodes: [docNode()],
			needs_attention: [{ kind: 'repair-failure', artifact_id: 'a1', reason: 'permission denied reading file', retryable: true }],

		});

		expect(host.textContent).toContain('Needs attention');
		expect(host.textContent).toContain('permission denied reading file');
		expect(host.querySelector('.lane-cap.needs-attention')).toBeTruthy();
	});

	it('cross-references the artifact name from nodes[] for a needs_attention row', () => {
		mountPane({
			nodes: [docNode({ id: 'a1', name: 'important.md' })],
			needs_attention: [{ kind: 'repair-failure', artifact_id: 'a1', reason: 'disk unavailable', retryable: true }],

		});
		expect(host.textContent).toContain('important.md');
	});

	it('falls back to a generic label when the artifact is not present in nodes', () => {
		mountPane({
			nodes: [], needs_attention: [{ kind: 'repair-failure', artifact_id: 'ghost', reason: 'gone', retryable: true }],

		});
		expect(host.textContent).toContain('Artifact');
		expect(host.textContent).toContain('gone');
	});

	it('Retry calls the explicit reconcile-apply endpoint, then reloads the pane', async () => {
		mountPane({
			nodes: [],
			needs_attention: [{ kind: 'repair-failure', artifact_id: 'a1', reason: 'permission denied', retryable: true }],

		});

		const retryBtn = [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Retry')!;
		retryBtn.click();

		await vi.waitFor(() => {
			expect(reconcileArtifactsApply).toHaveBeenCalledWith('sess-1');
		});
	});

	it('omits the Retry action when no entry is retryable', () => {
		mountPane({
			nodes: [],
			needs_attention: [{ kind: 'repair-failure', artifact_id: 'a1', reason: 'permission denied', retryable: false }],

		});
		const retryBtn = [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Retry');
		expect(retryBtn).toBeUndefined();
	});
});


describe('ArtifactPane — empty state', () => {
	it('shows the empty message when nodes and needs_attention are empty', () => {
		mountPane({ nodes: [], needs_attention: [] });
		expect(host.textContent).toContain('Files you create, upload, or open in this thread show up here.');
	});
});

describe('ArtifactPane — fetch failure is not "no artifacts"', () => {
	const EMPTY_COPY = 'Files you create, upload, or open in this thread show up here.';

	function mountLive() {
		component = mount(ArtifactPane, { target: host, props: { threadId: 'sess-1' } });
		flushSync();
	}

	it('renders a distinct error state with Retry, never the empty state', async () => {
		vi.mocked(fetchArtifactPane).mockRejectedValue(new Error('500'));

		mountLive();

		await vi.waitFor(() => {
			expect(host.querySelector('.pane-error')).toBeTruthy();
		});
		expect(host.textContent).toContain("Couldn't load this thread's files.");
		expect(host.textContent).not.toContain(EMPTY_COPY);
		expect([...host.querySelectorAll('button')].some((b) => b.textContent?.trim() === 'Retry')).toBe(true);
	});

	it('Retry refetches and shows the artifacts once the request succeeds', async () => {
		vi.mocked(fetchArtifactPane).mockRejectedValueOnce(new Error('500'));
		vi.mocked(fetchArtifactPane).mockResolvedValue({
			nodes: [docNode()], needs_attention: [],
		} as any);

		mountLive();
		await vi.waitFor(() => expect(host.querySelector('.pane-error')).toBeTruthy());

		[...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Retry')!.click();

		await vi.waitFor(() => {
			expect(host.textContent).toContain('notes.md');
		});
		expect(host.querySelector('.pane-error')).toBeNull();
	});

	it('keeps the last loaded rows and flags the stale refresh instead of blanking', async () => {
		vi.mocked(fetchArtifactPane).mockResolvedValueOnce({
			nodes: [docNode()], needs_attention: [],
		} as any);
		vi.mocked(fetchArtifactPane).mockRejectedValue(new Error('503'));

		mountLive();
		await vi.waitFor(() => expect(host.textContent).toContain('notes.md'));

		// Hiding a row reloads the pane; that reload is the one that fails.
		removeFromPane()!.click();

		await vi.waitFor(() => {
			expect(host.querySelector('.stale-notice')).toBeTruthy();
		});
		expect(host.textContent).toContain('notes.md');
		expect(host.textContent).not.toContain(EMPTY_COPY);
		expect(host.querySelector('.pane-error')).toBeNull();
	});
});

describe('ArtifactPane — group accessibility', () => {
	const group = {
		kind: 'worktree-group', key: '/repo/.worktrees/feat-foo', label: 'feat/foo',
		branch: 'feat/foo', file_count: 2, attention: false,
		children: [
			docNode({ id: 'c1', name: 'a.py', path: '/repo/.worktrees/feat-foo/a.py' }),
			docNode({ id: 'c2', name: 'b.py', path: '/repo/.worktrees/feat-foo/b.py' }),
		],
	};

	it('omits aria-controls while collapsed and points it at a real element when expanded', () => {
		mountPane({ nodes: [group], needs_attention: [] });

		const summary = host.querySelector<HTMLButtonElement>('.group-row')!;
		// A collapsed group renders no children container, so an aria-controls
		// here would be a dangling IDREF.
		expect(summary.hasAttribute('aria-controls')).toBe(false);

		summary.click();
		flushSync();

		const controls = summary.getAttribute('aria-controls');
		expect(controls).toBeTruthy();
		expect(document.getElementById(controls!)).toBeTruthy();
	});

	it('gives the bare child count an accessible name', () => {
		mountPane({ nodes: [group], needs_attention: [] });

		const summary = host.querySelector<HTMLButtonElement>('.group-row')!;
		expect(summary.textContent).toContain('2 files');
		expect(summary.querySelector('.group-count')!.getAttribute('aria-hidden')).toBe('true');
	});

	it('announces the attention dot rather than relying on colour alone', () => {
		mountPane({ nodes: [{ ...group, attention: true }], needs_attention: [] });

		const summary = host.querySelector<HTMLButtonElement>('.group-row')!;
		expect(summary.querySelector('.dot.attention')!.getAttribute('aria-hidden')).toBe('true');
		expect(summary.textContent).toContain('needs attention');
	});

	it('opens a still-failing group only after the user explicitly retries', async () => {
		mountPane({
			nodes: [{ ...group, attention: true }],
			needs_attention: [{ kind: 'repair-failure', artifact_id: 'c1', reason: 'permission denied', retryable: true }],

		});

		const summary = host.querySelector<HTMLButtonElement>('.group-row')!;
		expect(summary.getAttribute('aria-expanded')).toBe('false');

		[...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Retry')!.click();

		await vi.waitFor(() => {
			expect(reconcileArtifactsApply).toHaveBeenCalledWith('sess-1');
		});
		await vi.waitFor(() => {
			expect(
				host.querySelector<HTMLButtonElement>('.group-row')!.getAttribute('aria-expanded')
			).toBe('true');
		});
		expect(host.textContent).toContain('a.py');
	});
});
