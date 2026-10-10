import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import DocFeedbackStatus from './DocFeedbackStatus.svelte';

let host: HTMLElement;
let component: { commentCreated: () => void; sync: (id: string, feedback: { open_count: number; owner_label: string | null; grace_closes_at: string | null; undelivered_batches: any[] }) => void } | null = null;
const fetchMock = vi.fn();
const onFeedbackChanged = vi.fn();

function result(undelivered_batches: unknown[] = []) {
	return new Response(JSON.stringify({ woken: [], undelivered_batches }), { status: 200 });
}

function render() {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocFeedbackStatus, {
		target: host,
		props: { artifactId: 'artifact-1', targetInstanceId: 'tab-1', onFeedbackChanged },
	});
	flushSync();
}

function clearToasts() {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
}

function setVisibility(value: 'visible' | 'hidden') {
	Object.defineProperty(document, 'visibilityState', { configurable: true, value });
}

beforeEach(() => {
	clearToasts();
	setVisibility('visible');
	fetchMock.mockReset();
	fetchMock.mockImplementation(() => Promise.resolve(result()));
	onFeedbackChanged.mockReset();
	vi.stubGlobal('fetch', fetchMock);
	token.set('test-token');
	render();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	token.set(null);
	clearToasts();
});

describe('DocFeedbackStatus', () => {
	it('sends pending comments with the bearer header, then hides the control', async () => {
		fetchMock.mockImplementation(() => Promise.resolve(result()));
		component!.commentCreated();
		flushSync();

		expect(host.textContent).toContain('Send 1 comment');
		host.querySelector<HTMLButtonElement>('button')!.click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
		flushSync();

		expect(fetchMock).toHaveBeenCalledWith('/api/doc-feedback/send', expect.objectContaining({
			keepalive: false,
			headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
		}));
		await vi.waitFor(() => expect(host.textContent).not.toContain('Send 1 comment'));
	});

	it('shows an undelivered reason and clears it after Retry', async () => {
		fetchMock.mockResolvedValueOnce(result([{ id: 'batch-1', item_count: 3, undelivered_reason: 'owner_unavailable', attempts: 1 }]));
		fetchMock.mockResolvedValueOnce(result());
		component!.commentCreated();
		flushSync();
		host.querySelector<HTMLButtonElement>('button')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('3 comments not delivered: its owner is unavailable'));

		host.querySelector<HTMLButtonElement>('button')!.click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
		flushSync();

		expect(fetchMock.mock.calls[1][0]).toBe('/api/doc-feedback/retry');
		await vi.waitFor(() => expect(host.textContent).not.toContain('not delivered'));
	});

	it('keeps one status region mounted and changes only its text', async () => {
		const region = host.querySelector('[role="status"]');
		expect(region).not.toBeNull();
		expect(region!.textContent).toBe('');
		fetchMock.mockResolvedValueOnce(result([{ id: 'batch-1', item_count: 1, undelivered_reason: 'owner_unaddressable', attempts: 1 }]));
		component!.commentCreated();
		flushSync();
		host.querySelector<HTMLButtonElement>('button')!.click();

		await vi.waitFor(() => expect(region!.textContent).toBe("1 comment not delivered: its owner can't be reached"));
		expect(host.querySelectorAll('[role="status"]')).toHaveLength(1);
		expect(host.querySelector('[role="status"]')).toBe(region);
	});

	it('seeds the durable count, owner label, and undelivered state from comments GET', () => {
		component!.sync('artifact-1', {
			open_count: 4,
			owner_label: 'Documents & Artifacts',
			grace_closes_at: null,
			undelivered_batches: [{ id: 'batch-1', item_count: 3, undelivered_reason: 'owner_unavailable', attempts: 1 }],
		});
		flushSync();

		expect(host.textContent).toContain('Send 4 comments to Documents & Artifacts');
		expect(host.textContent).toContain('3 comments not delivered: its owner is unavailable');
		component!.sync('artifact-1', { open_count: 0, owner_label: null, grace_closes_at: null, undelivered_batches: [] });
	});

	it('shows the server grace deadline and sends now through the existing action', async () => {
		const deadline = new Date(Date.now() + 61_000).toISOString();
		component!.sync('artifact-1', {
			open_count: 2,
			owner_label: 'Documents & Artifacts',
			grace_closes_at: deadline,
			undelivered_batches: [],
		});
		flushSync();

		expect(host.textContent).toContain('Sending to Documents & Artifacts in 2 min');
		expect(host.textContent).not.toContain('Send 2 comments');
		host.querySelector<HTMLButtonElement>('button')!.click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/doc-feedback/send', expect.anything()));
	});

	it('keeps pending comments visible and shows a toast when Send fails', async () => {
		fetchMock.mockRejectedValue(new Error('offline'));
		component!.sync('artifact-1', { open_count: 1, owner_label: null, grace_closes_at: null, undelivered_batches: [] });
		flushSync();
		host.querySelector<HTMLButtonElement>('button')!.click();

		await vi.waitFor(() => expect(get(toasts).map((toast) => toast.message)).toContain("Couldn't send your comments — try again"));
		expect(host.textContent).toContain('Send 1 comment');
		component!.sync('artifact-1', { open_count: 0, owner_label: null, grace_closes_at: null, undelivered_batches: [] });
	});

	it('sends leave once with keepalive when the page hides or unloads', async () => {
		fetchMock.mockImplementation(() => Promise.resolve(result()));
		component!.commentCreated();
		Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
		window.dispatchEvent(new Event('visibilitychange'));
		window.dispatchEvent(new Event('beforeunload'));
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		expect(fetchMock).toHaveBeenCalledWith('/api/doc-feedback/leave', expect.objectContaining({
			keepalive: true,
			headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
		}));
	});

	it('retries leave after its request fails', async () => {
		const error = vi.spyOn(console, 'error').mockImplementation(() => {});
		setVisibility('hidden');
		fetchMock.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(result());
		component!.commentCreated();
		window.dispatchEvent(new Event('beforeunload'));
		await vi.waitFor(() => expect(error).toHaveBeenCalledOnce());
		component!.sync('artifact-1', { open_count: 1, owner_label: null, grace_closes_at: null, undelivered_batches: [] });
		window.dispatchEvent(new Event('beforeunload'));
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
	});

	it.each([
		['no_owner', 'this document has no owner'],
		['owner_archived', 'its owner is archived'],
		['owner_unavailable', 'its owner is unavailable'],
		['owner_admission_refused', "its owner couldn't take it right now"],
		['owner_unaddressable', "its owner can't be reached"],
		['future_reason', "it couldn't be delivered"],
	])('maps %s without rendering a raw reason code', (code, text) => {
		component!.sync('artifact-1', {
			open_count: 0,
			owner_label: null,
			grace_closes_at: null,
			undelivered_batches: [{ id: 'batch-1', item_count: 1, undelivered_reason: code, attempts: 1 }],
		});
		flushSync();

		expect(host.textContent).toContain(text);
		expect(host.textContent).not.toContain(code);
		expect(host.querySelector('[role="status"] button')).toBeNull();
	});

	it('omits the reason when undelivered batches disagree', () => {
		component!.sync('artifact-1', {
			open_count: 0,
			owner_label: null,
			grace_closes_at: null,
			undelivered_batches: [
				{ id: 'batch-1', item_count: 1, undelivered_reason: 'owner_unavailable', attempts: 1 },
				{ id: 'batch-2', item_count: 2, undelivered_reason: 'no_owner', attempts: 1 },
			],
		});
		flushSync();

		expect(host.textContent).toContain('3 comments not delivered');
		expect(host.textContent).not.toContain('not delivered:');
	});

	it('reports the user returned when fresh state reaches a visible document', async () => {
		component!.sync('artifact-1', { open_count: 1, owner_label: null, grace_closes_at: null, undelivered_batches: [] });
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		expect(fetchMock).toHaveBeenCalledWith('/api/doc-feedback/return', expect.objectContaining({
			body: JSON.stringify({ artifact_id: 'artifact-1' }),
			headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
		}));
	});

	it('does not report a return when a hidden tab refreshes its comments', async () => {
		setVisibility('hidden');
		component!.sync('artifact-1', { open_count: 1, owner_label: null, grace_closes_at: null, undelivered_batches: [] });
		await Promise.resolve();

		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('re-syncs through its existing owner callback when the page becomes visible', () => {
		Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
		window.dispatchEvent(new Event('visibilitychange'));

		expect(onFeedbackChanged).toHaveBeenCalledOnce();
	});
});
