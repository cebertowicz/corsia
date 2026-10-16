/**
 * pane-conductor-render.test.ts
 *
 * Invariants after unified-conductor-pane-ia (and the artifact-pane-hierarchy cutover):
 *
 *   A) Pane.svelte renders NO .bare-section and no standalone 'Requires disposition' header.
 *      The attention-conductor section kind is gone from CONDUCTOR_SECTIONS.
 *      All sections go through the standard .cap / .card path.
 *
 *   B) ArtifactPane in conductor mode calls fetchRaised(token, conductorThreadId).
 *      This is now done inside ArtifactPane, not via AttentionConductorPane.
 *
 *   C) "Needs you" is driven solely by raised disposition requests (fetchRaised) — the
 *      hierarchical pane DTO carries no equivalent "actionable artifact" list; its own
 *      needs_attention[] repair-failure cluster is a distinct, never-merged section.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { _resetProjectsForTests } from '$lib/stores/projects';
import { activeThreadId } from '$lib/stores/threads';
import { token } from '$lib/stores/auth';
import { paneOpen } from '$lib/stores/paneOpen';
import Pane from '$lib/components/Pane.svelte';
import ArtifactPane from '$lib/components/ArtifactPane.svelte';
import { emit } from '$lib/ws/events';

vi.mock('$lib/api/attention', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/api/attention')>();
	return { ...original, fetchRaised: vi.fn().mockResolvedValue({ items: [] }) };
});

import { fetchRaised } from '$lib/api/attention';

const DEMO_PANE_EMPTY = { nodes: [], needs_attention: [], total: 0 };

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
		ok: true,
		json: async () => DEMO_PANE_EMPTY,
		text: async () => '{}',
	}));
	// Reset implementation AND call history so overrides from prior tests don't leak.
	vi.mocked(fetchRaised).mockReset();
	vi.mocked(fetchRaised).mockResolvedValue({ items: [] });
	token.set(null);
	_resetProjectsForTests();
	activeThreadId.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(async () => {
	if (component) unmount(component);
	component = null;
	// Drain pending microtasks from async load() so they don't bleed into the next test.
	await new Promise((r) => setTimeout(r, 100));
	host.remove();
	token.set(null);
	paneOpen.set(false);
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	vi.mocked(fetchRaised).mockReset();
});

describe('Pane — mobile drawer control ownership', () => {
	it('does not render a pane-owned close button; the shell header owns drawer toggling', () => {
		Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
		paneOpen.set(true);

		component = mount(Pane, {
			target: host,
			props: {
				label: 'Workspace',
				sections: [{ title: 'Roadmap', empty: 'No roadmap yet' }],
			},
		});
		flushSync();

		const close = host.querySelector<HTMLButtonElement>('button[aria-label="Close panel"]');
		expect(close).toBeNull();
		expect(get(paneOpen)).toBe(true);
	});
});

// ── A. No standalone bare-section / Requires disposition ─────────────────────

describe('Pane — no standalone attention-conductor section', () => {
	it('renders NO .bare-section wrapper (attention-conductor removed from sections)', () => {
		component = mount(Pane, {
			target: host,
			props: {
				sections: [{ title: 'Artifacts', kind: 'artifacts' }],
				conductorThreadId: 'cond-123',
			},
		});
		flushSync();

		expect(host.querySelector('.bare-section')).toBeNull();
	});

	it('does NOT render a standalone "Requires disposition" header', () => {
		component = mount(Pane, {
			target: host,
			props: {
				sections: [{ title: 'Artifacts', kind: 'artifacts' }],
				conductorThreadId: 'cond-123',
			},
		});
		flushSync();

		expect(host.textContent).not.toContain('Requires disposition');
	});

	it('all sections use .cap button header (no bare-section exception)', () => {
		component = mount(Pane, {
			target: host,
			props: {
				sections: [{ title: 'Draft a plan' }, { title: 'Artifacts', kind: 'artifacts' }],
			},
		});
		flushSync();

		const caps = host.querySelectorAll('button.cap');
		expect(caps.length).toBeGreaterThanOrEqual(2);
		expect(host.querySelector('.bare-section')).toBeNull();
	});
});

// ── B. ArtifactPane conductor mode calls fetchRaised ────────────────────────
// Note: these tests do NOT use `demo` so that the real load() path runs and we can
// verify fetchRaised behavior. The global `fetch` stub returns DEMO_PANE_EMPTY by default.

describe('ArtifactPane — fetchRaised called in conductor mode', () => {
	it('calls fetchRaised(tok, conductorThreadId) when in conductor mode', async () => {
		token.set('tok');

		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'cond-123', conductorThreadId: 'cond-123' },
		});
		flushSync();

		await vi.waitFor(() => {
			expect(vi.mocked(fetchRaised)).toHaveBeenCalledWith('tok', 'cond-123');
		});
	});

	it('refetches raised items when plans change', async () => {
		token.set('tok');
		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'cond-123', conductorThreadId: 'cond-123' },
		});
		flushSync();
		await vi.waitFor(() => {
			expect(vi.mocked(fetchRaised)).toHaveBeenCalledWith('tok', 'cond-123');
		});
		vi.mocked(fetchRaised).mockClear();

		emit('plans_changed', { type: 'plans_changed', plans: [] });

		await vi.waitFor(() => {
			expect(vi.mocked(fetchRaised)).toHaveBeenCalledWith('tok', 'cond-123');
		});
	});

	it('does NOT call fetchRaised when not in conductor mode (threadId differs)', async () => {
		token.set('tok');

		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'other-session', conductorThreadId: 'cond-123' },
		});
		flushSync();

		// Wait for load() to complete — the empty-state text appears when loaded=true and data is empty.
		await vi.waitFor(() => {
			expect(host.textContent).toContain('Files you create');
		});
		expect(vi.mocked(fetchRaised)).not.toHaveBeenCalled();
	});

	it('does NOT call fetchRaised when conductorThreadId absent', async () => {
		token.set('tok');

		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'cond-123' },
		});
		flushSync();

		// Wait for load() to complete — the empty-state text appears when loaded=true and data is empty.
		await vi.waitFor(() => {
			expect(host.textContent).toContain('Files you create');
		});
		expect(vi.mocked(fetchRaised)).not.toHaveBeenCalled();
	});
});

describe('ArtifactPane — artifact identity refresh', () => {
	it('refreshes only panes affected by the changed artifact identity', async () => {
		token.set('tok');
		const fetchMock = vi.mocked(fetch);
		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'owner-123' },
		});
		flushSync();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
		const initialCalls = fetchMock.mock.calls.length;

		emit('artifacts_changed', {
			artifact_id: 'artifact-1',
			owner_thread_id: 'other-owner',
			affected_thread_ids: ['other-owner'],
		});
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(fetchMock).toHaveBeenCalledTimes(initialCalls);

		emit('artifacts_changed', {
			artifact_id: 'artifact-1',
			owner_thread_id: 'owner-123',
			affected_thread_ids: ['owner-123'],
		});
		await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(initialCalls));
		const afterArtifactRefresh = fetchMock.mock.calls.length;

		emit('turn_complete', { thread_id: 'other-owner' });
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(fetchMock).toHaveBeenCalledTimes(afterArtifactRefresh);

		emit('turn_complete', { thread_id: 'owner-123' });
		await vi.waitFor(() =>
			expect(fetchMock.mock.calls.length).toBeGreaterThan(afterArtifactRefresh)
		);
	});
});

// ── C. "Needs you" driven solely by raised disposition requests ─────────────

describe('ArtifactPane — "Needs you" queue is raised-only', () => {
	it('renders exactly one "Needs you" header when fetchRaised returns items', async () => {
		token.set('tok');
		vi.mocked(fetchRaised).mockResolvedValue({
			items: [{ external_id: 'raise-1', attention_state: 'raised', integration: 'local', type: 'user-raise', levels: [] }],
		});

		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'cond-123', conductorThreadId: 'cond-123' },
		});
		flushSync();
		await vi.waitFor(() => expect(vi.mocked(fetchRaised)).toHaveBeenCalled());
		flushSync();

		const needsHeaders = [...host.querySelectorAll('.lane-cap')].filter(
			(el) => el.textContent?.toLowerCase().includes('needs you')
		);
		expect(needsHeaders).toHaveLength(1);
	});

	it('hides "Needs you" header when raised is empty', async () => {
		token.set('tok');

		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'cond-123', conductorThreadId: 'cond-123' },
		});
		flushSync();
		await vi.waitFor(() => expect(vi.mocked(fetchRaised)).toHaveBeenCalled());
		flushSync();

		const needsHeaders = [...host.querySelectorAll('.lane-cap')].filter(
			(el) => el.textContent?.toLowerCase().includes('needs you')
		);
		expect(needsHeaders).toHaveLength(0);
	});

	it('does NOT render "Needs you" outside conductor mode even with needs_attention present', async () => {
		token.set('tok');
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({
				nodes: [],
				needs_attention: [{ kind: 'repair-failure', artifact_id: 'a1', reason: 'permission denied', retryable: true }],
				total: 0,
			}),
			text: async () => '{}',
		}));

		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'sess-1' },
		});
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Needs attention'));

		const needsYouHeaders = [...host.querySelectorAll('.lane-cap')].filter(
			(el) => el.textContent?.toLowerCase() === 'needs you'
		);
		expect(needsYouHeaders).toHaveLength(0);
	});
});
