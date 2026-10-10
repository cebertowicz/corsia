import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchArtifactPane } from '$lib/api/docs';
import ArtifactPane from './ArtifactPane.svelte';
import { token, user } from '$lib/stores/auth';
import { emit } from '$lib/ws/events';

vi.mock('$lib/api/docs', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/api/docs')>();
	return {
		...original,
		setArtifactDisplay: vi.fn().mockResolvedValue({}),
		reconcileArtifactsApply: vi.fn().mockResolvedValue({
			ok: true, mode: 'apply', counts: {}, receipts: [],
		}),
		fetchArtifactPane: vi.fn(),
	};
});

const CONTROL = {
	kind: 'artifact',
	id: 'control-1',
	integration: 'local',
	type: 'plan-node',
	external_id: 'node-1',
	title: 'Blocked work',
	name: 'Blocked work',
	path: null,
	retention: 'ephemeral',
	surfacing: 'visible',
	pinned: false, pin_eligible: true, retirement_exempt: false,
	state: 'active',
	updated_at: '2026-09-09T00:00:00Z',
	description: null,
	metadata: {},
	presentation: { label: 'Plan node', icon: 'list-checks', capability: 'leaf-package' },
	action: { capability: 'leaf-package', disabled: false, disabled_reason: null },
};

const EMPTY = { nodes: [], needs_attention: [] };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('tok');
	user.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
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

async function settle() {
	await tick();
	await Promise.resolve();
	flushSync();
}

describe('ArtifactPane reconnect refresh', () => {
	it('refetches canonical pane state for the backend artifact-change frame', async () => {
		vi.mocked(fetchArtifactPane)
			.mockResolvedValueOnce({ ...EMPTY, nodes: [CONTROL] } as never)
			.mockResolvedValueOnce(EMPTY as never);
		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'thread-1' },
		});
		await vi.waitFor(() => expect(host.textContent).toContain('Blocked work'));

		emit('artifacts_changed', {
			artifact_id: 'artifact-1',
			owner_thread_id: 'thread-1',
			affected_thread_ids: ['thread-1'],
		});

		await vi.waitFor(() => {
			expect(fetchArtifactPane).toHaveBeenCalledTimes(2);
			expect(host.textContent).not.toContain('Blocked work');
		});
	});

	it('refetches canonical pane state after authenticated reconnect', async () => {
		vi.mocked(fetchArtifactPane)
			.mockResolvedValueOnce({ ...EMPTY, nodes: [CONTROL] } as never)
			.mockResolvedValueOnce(EMPTY as never);
		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'thread-1' },
		});
		await vi.waitFor(() => expect(host.textContent).toContain('Blocked work'));

		emit('connected', {});

		await vi.waitFor(() => {
			expect(fetchArtifactPane).toHaveBeenCalledTimes(2);
			expect(host.textContent).not.toContain('Blocked work');
		});
	});

	it('ignores an older response that finishes after reconnect refresh', async () => {
		let finishInitial!: (value: typeof EMPTY) => void;
		const initial = new Promise<typeof EMPTY>((resolve) => {
			finishInitial = resolve;
		});
		vi.mocked(fetchArtifactPane)
			.mockReturnValueOnce(initial as never)
			.mockResolvedValueOnce(EMPTY as never);
		component = mount(ArtifactPane, {
			target: host,
			props: { threadId: 'thread-1' },
		});
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		emit('connected', {});
		await vi.waitFor(() => {
			expect(fetchArtifactPane).toHaveBeenCalledTimes(2);
			expect(host.textContent).toContain('Files you create, upload, or open');
		});

		finishInitial({ ...EMPTY, nodes: [CONTROL] } as never);
		await settle();
		expect(host.textContent).not.toContain('Blocked work');
	});

	it('answers a burst of artifact-change frames with one request in flight and one trailing refetch', async () => {
		let finishInitial!: (value: typeof EMPTY) => void;
		const initial = new Promise<typeof EMPTY>((resolve) => {
			finishInitial = resolve;
		});
		vi.mocked(fetchArtifactPane)
			.mockReturnValueOnce(initial as never)
			.mockResolvedValueOnce({ ...EMPTY, nodes: [CONTROL] } as never);
		component = mount(ArtifactPane, { target: host, props: { threadId: 'thread-1' } });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		for (let i = 0; i < 47; i++) {
			emit('artifacts_changed', {
				artifact_id: `artifact-${i}`,
				owner_thread_id: 'thread-1',
				affected_thread_ids: ['thread-1'],
			});
		}
		await settle();
		expect(fetchArtifactPane).toHaveBeenCalledTimes(1);

		finishInitial(EMPTY);
		await vi.waitFor(() => expect(host.textContent).toContain('Blocked work'));
		await settle();
		expect(fetchArtifactPane).toHaveBeenCalledTimes(2);
	});

	it('keeps refetching for frames that arrive after a refetch has started', async () => {
		vi.mocked(fetchArtifactPane).mockResolvedValue(EMPTY as never);
		component = mount(ArtifactPane, { target: host, props: { threadId: 'thread-1' } });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		for (let round = 1; round <= 3; round++) {
			emit('artifacts_changed', {
				artifact_id: 'artifact-1',
				owner_thread_id: 'thread-1',
				affected_thread_ids: ['thread-1'],
			});
			await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1 + round));
		}
	});

	it('keeps the reply marker after opening until the server confirms disclosure', async () => {
		component = mount(ArtifactPane, {
			target: host,
			props: {
				threadId: 'thread-1',
				demo: { ...EMPTY, nodes: [{ ...CONTROL, name: 'Document', path: '/tmp/document.md', reply_waiting: true }] } as never,
			},
		});
		await settle();
		expect(host.querySelector('.reply-mark')).not.toBeNull();

		host.querySelector<HTMLButtonElement>('button.row.openable')!.click();
		flushSync();
		expect(host.querySelector('.reply-mark')).not.toBeNull();
	});

	it('refetches when a comment changes on a shown document, so its reply marker clears', async () => {
		const waiting = { ...CONTROL, id: 'doc-1', name: 'Document', path: '/tmp/document.md', reply_waiting: true };
		vi.mocked(fetchArtifactPane)
			.mockResolvedValueOnce({ ...EMPTY, nodes: [waiting] } as never)
			.mockResolvedValueOnce({ ...EMPTY, nodes: [{ ...waiting, reply_waiting: false }] } as never);
		component = mount(ArtifactPane, { target: host, props: { threadId: 'thread-1' } });
		await vi.waitFor(() => expect(host.querySelector('.reply-mark')).not.toBeNull());

		emit('doc_comment_changed', { artifact_id: 'doc-1' });

		await vi.waitFor(() => {
			expect(fetchArtifactPane).toHaveBeenCalledTimes(2);
			expect(host.querySelector('.reply-mark')).toBeNull();
		});
	});

	it('refetches for a document inside a worktree group and ignores one the pane does not show', async () => {
		const child = { ...CONTROL, id: 'doc-2', name: 'Grouped', path: '/tmp/grouped.md' };
		const group = { kind: 'worktree-group', key: 'wt', label: 'wt', branch: 'wt', file_count: 1, attention: false, children: [child] };
		vi.mocked(fetchArtifactPane).mockResolvedValue({ ...EMPTY, nodes: [group] } as never);
		component = mount(ArtifactPane, { target: host, props: { threadId: 'thread-1' } });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		emit('doc_comment_changed', { artifact_id: 'elsewhere' });
		await settle();
		expect(fetchArtifactPane).toHaveBeenCalledTimes(1);

		emit('doc_comment_changed', { artifact_id: 'doc-2' });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(2));
	});
});
