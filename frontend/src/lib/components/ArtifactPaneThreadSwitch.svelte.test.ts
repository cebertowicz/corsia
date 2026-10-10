import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchArtifactPane } from '$lib/api/docs';
import ArtifactPane from './ArtifactPane.svelte';
import { token, user } from '$lib/stores/auth';
import { emit } from '$lib/ws/events';
import { failThreadOpen } from '$lib/openTimingRuntime';
import { toasts } from '$lib/stores/toast';

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

vi.mock('$lib/openTimingRuntime', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/openTimingRuntime')>();
	return { ...original, failThreadOpen: vi.fn() };
});

const row = (id: string, name: string) => ({
	kind: 'artifact', id, integration: 'local', type: 'plan-node', external_id: id,
	title: name, name, path: null, retention: 'ephemeral', surfacing: 'visible',
	pinned: false, pin_eligible: true, retirement_exempt: false, state: 'active',
	updated_at: '2026-09-09T00:00:00Z', description: null, metadata: {},
	presentation: { label: 'Plan node', icon: 'list-checks', capability: 'leaf-package' },
	action: { capability: 'leaf-package', disabled: false, disabled_reason: null },
});
const paneOf = (...rows: ReturnType<typeof row>[]) => ({ nodes: rows, needs_attention: [] });
const EMPTY = paneOf();

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let props = $state<{ threadId: string }>({ threadId: 'thread-a' });

beforeEach(() => {
	token.set('tok');
	user.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
	vi.mocked(fetchArtifactPane).mockReset();
	props = { threadId: 'thread-a' };
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

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((r) => {
		resolve = r;
	});
	return { promise, resolve };
}

const frame = (thread: string, id = 'x') =>
	emit('artifacts_changed', { artifact_id: id, owner_thread_id: thread, affected_thread_ids: [thread] });
const calledThreads = () => vi.mocked(fetchArtifactPane).mock.calls.map(([id]) => id);
const button = (label: string) =>
	[...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!;

describe('ArtifactPane read slot', () => {
	it('reads the new thread at once while the old thread is still being read', async () => {
		const a = deferred<unknown>();
		vi.mocked(fetchArtifactPane)
			.mockReturnValueOnce(a.promise as never)
			.mockResolvedValueOnce(paneOf(row('b-1', 'Thread B file')) as never);
		component = mount(ArtifactPane, { target: host, props });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		props.threadId = 'thread-b';
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(2));
		expect(calledThreads()).toEqual(['thread-a', 'thread-b']);
		await vi.waitFor(() => expect(host.textContent).toContain('Thread B file'));

		// The old thread's late response is discarded and starts nothing.
		a.resolve(paneOf(row('a-1', 'Thread A file')));
		await settle();
		expect(host.textContent).not.toContain('Thread A file');
		expect(host.textContent).toContain('Thread B file');
		expect(fetchArtifactPane).toHaveBeenCalledTimes(2);
	});

	it('does not read the old thread again for a frame that arrived before the switch', async () => {
		const a = deferred<unknown>();
		vi.mocked(fetchArtifactPane)
			.mockReturnValueOnce(a.promise as never)
			.mockResolvedValue(EMPTY as never);
		component = mount(ArtifactPane, { target: host, props });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		frame('thread-a');
		props.threadId = 'thread-b';
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(2));

		a.resolve(EMPTY);
		await settle();
		await settle();
		expect(calledThreads()).toEqual(['thread-a', 'thread-b']);
	});

	it('makes 3 requests, not 4, when the pane switches A to B and back to A with a frame queued for A', async () => {
		const a1 = deferred<unknown>();
		const a2 = deferred<unknown>();
		vi.mocked(fetchArtifactPane)
			.mockReturnValueOnce(a1.promise as never)
			.mockResolvedValueOnce(EMPTY as never)
			.mockReturnValueOnce(a2.promise as never)
			.mockResolvedValue(EMPTY as never);
		component = mount(ArtifactPane, { target: host, props });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		frame('thread-a');
		props.threadId = 'thread-b';
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(2));
		props.threadId = 'thread-a';
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(3));

		// The first read of A settles while the read made on the way back is in flight.
		a1.resolve(paneOf(row('a-old', 'Old A file')));
		await settle();
		await settle();
		expect(calledThreads()).toEqual(['thread-a', 'thread-b', 'thread-a']);

		a2.resolve(paneOf(row('a-new', 'New A file')));
		await settle();
		expect(host.textContent).toContain('New A file');
		expect(host.textContent).not.toContain('Old A file');
		expect(fetchArtifactPane).toHaveBeenCalledTimes(3);
	});

	it('still answers a burst on the new thread with one trailing read', async () => {
		const b = deferred<unknown>();
		vi.mocked(fetchArtifactPane)
			.mockResolvedValueOnce(EMPTY as never)
			.mockReturnValueOnce(b.promise as never)
			.mockResolvedValue(EMPTY as never);
		component = mount(ArtifactPane, { target: host, props });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		props.threadId = 'thread-b';
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(2));
		for (let i = 0; i < 47; i++) frame('thread-b', `x-${i}`);
		await settle();
		expect(fetchArtifactPane).toHaveBeenCalledTimes(2);

		b.resolve(EMPTY);
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(3));
		await settle();
		expect(fetchArtifactPane).toHaveBeenCalledTimes(3);
		expect(calledThreads()).toEqual(['thread-a', 'thread-b', 'thread-b']);
	});

	it('reads at once on reconnect while a frame-started read never settles', async () => {
		vi.mocked(fetchArtifactPane)
			.mockResolvedValueOnce(EMPTY as never)
			.mockReturnValueOnce(new Promise(() => {}) as never)
			.mockResolvedValue(paneOf(row('a-1', 'After reconnect')) as never);
		component = mount(ArtifactPane, { target: host, props });
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(1));

		frame('thread-a');
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(2));
		frame('thread-a', 'y');
		await settle();
		expect(fetchArtifactPane).toHaveBeenCalledTimes(2);

		emit('connected', {});
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(3));
		await vi.waitFor(() => expect(host.textContent).toContain('After reconnect'));
	});

	it('reads at once when Retry is pressed while a frame-started read never settles', async () => {
		vi.mocked(fetchArtifactPane)
			.mockRejectedValueOnce(new Error('500'))
			.mockReturnValueOnce(new Promise(() => {}) as never)
			.mockResolvedValue(paneOf(row('a-1', 'After retry')) as never);
		component = mount(ArtifactPane, { target: host, props });
		await vi.waitFor(() => expect(host.querySelector('.pane-error')).toBeTruthy());

		frame('thread-a');
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(2));

		button('Retry').click();
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledTimes(3));
		await vi.waitFor(() => expect(host.textContent).toContain('After retry'));
	});

	it('rejects the reconcile Retry when its reload fails, so the failure toast shows', async () => {
		vi.mocked(fetchArtifactPane).mockResolvedValue({
			nodes: [],
			needs_attention: [{ kind: 'repair-failure', artifact_id: 'a1', reason: 'denied', retryable: true }],
		} as never);
		component = mount(ArtifactPane, { target: host, props });
		await vi.waitFor(() => expect(button('Retry')).toBeTruthy());

		// loadPane handles a failed request itself; only a throw from its own handler escapes.
		vi.mocked(fetchArtifactPane).mockRejectedValueOnce(new Error('500'));
		vi.mocked(failThreadOpen).mockImplementationOnce(() => {
			throw new Error('timing runtime failed');
		});
		button('Retry').click();
		await vi.waitFor(() =>
			expect(get(toasts).some((t) => t.message.startsWith('Retry failed'))).toBe(true),
		);
	});
});
