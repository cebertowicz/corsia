import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { SENT_HOLD_MS } from '$lib/docFeedback.svelte';
import DocFeedbackStatus from './DocFeedbackStatus.svelte';

let host: HTMLElement;
let component: { commentCreated: () => void; sync: (id: string, feedback: { open_count: number; owner_label: string | null; undelivered_batches: any[] }) => void } | null = null;
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

/** The server reports a message waiting to be sent: a re-opened comment or an edit, since a new comment goes at once. */
function waitToSend(count = 1) {
	component!.sync('artifact-1', { open_count: count, owner_label: null, undelivered_batches: [] });
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
		waitToSend();
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

	it('confirms a delivered send with Sent and a check for a moment, then lets the control go', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout'] });
		try {
			waitToSend();
			flushSync();
			host.querySelector<HTMLButtonElement>('button')!.click();
			await vi.waitFor(() => expect(host.querySelector('.sent')).not.toBeNull());

			const sent = host.querySelector('.sent')!;
			expect(sent.textContent!.trim()).toBe('Sent');
			expect(sent.querySelector('svg')).not.toBeNull();
			expect(host.querySelector('button')).toBeNull();
			expect(host.querySelector('[role="status"]')!.textContent).toBe('1 comment sent');

			vi.advanceTimersByTime(SENT_HOLD_MS);
			flushSync();
			expect(host.querySelector('.sent')).toBeNull();
			expect(host.querySelector('.sending-now')).toBeNull();
			expect(host.querySelector('[role="status"]')!.textContent).toBe('');
		} finally {
			vi.useRealTimers();
		}
	});

	it('says nothing was sent when the batch could not be delivered', async () => {
		fetchMock.mockResolvedValueOnce(result([{ id: 'batch-1', item_count: 1, undelivered_reason: 'no_owner', attempts: 1 }]));
		waitToSend();
		flushSync();
		host.querySelector<HTMLButtonElement>('button')!.click();

		await vi.waitFor(() => expect(host.textContent).toContain('1 comment not delivered'));
		expect(host.querySelector('.sent')).toBeNull();
	});

	it('Send grows in when the first comment arrives and Sent leaves the way it came, not by growing again', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout'] });
		const animate = vi.spyOn(Element.prototype, 'animate');
		// The slot's own motion, told from Svelte's timing animations by the space it opens and closes.
		const slotMotion = () => animate.mock.calls.map(([frames]) => frames as Keyframe[]).filter((frames) => Array.isArray(frames) && frames.some((frame) => 'maxWidth' in frame));
		try {
			waitToSend();
			flushSync();
			expect(slotMotion()).toHaveLength(1);
			expect(slotMotion()[0][0]).toMatchObject({ maxWidth: '0px', opacity: 0 });

			host.querySelector<HTMLButtonElement>('button')!.click();
			await vi.waitFor(() => expect(host.querySelector('.sent')).not.toBeNull());
			vi.advanceTimersByTime(SENT_HOLD_MS);
			flushSync();
			const leaving = slotMotion().at(-1)!;
			expect(slotMotion()).toHaveLength(2);
			expect(leaving[0]).toMatchObject({ opacity: 1 });
			expect(leaving.at(-1)).toMatchObject({ maxWidth: '0px', opacity: 0 });
		} finally {
			animate.mockRestore();
			vi.useRealTimers();
		}
	});

	it('a send that is not delivered swaps Send for Retry at once, never both in the row while Send closes', async () => {
		const animate = vi.spyOn(Element.prototype, 'animate');
		const slotMotion = () => animate.mock.calls.map(([frames]) => frames as Keyframe[]).filter((frames) => Array.isArray(frames) && frames.some((frame) => 'maxWidth' in frame));
		try {
			waitToSend();
			flushSync();
			expect(slotMotion()).toHaveLength(1);
			fetchMock.mockResolvedValueOnce(result([{ id: 'batch-1', item_count: 1, undelivered_reason: 'no_owner', attempts: 1 }]));
			host.querySelector<HTMLButtonElement>('button')!.click();
			await vi.waitFor(() => expect(host.querySelector('.undelivered .btn, .undelivered button')).not.toBeNull());
			flushSync();
			expect(slotMotion()).toHaveLength(1);
			expect(host.querySelector('.sending-now')).toBeNull();
		} finally {
			animate.mockRestore();
		}
	});

	it('in a narrow bar, a batch not delivered is one Retry in Send\'s place, the summary read out rather than shown', async () => {
		unmount(component!);
		host.remove();
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(DocFeedbackStatus, { target: host, props: { artifactId: 'artifact-1', targetInstanceId: 'tab-1', onFeedbackChanged, compact: true } });
		flushSync();
		fetchMock.mockResolvedValueOnce(result([{ id: 'batch-1', item_count: 1, undelivered_reason: 'no_owner', attempts: 1 }]));
		waitToSend();
		flushSync();
		host.querySelector<HTMLButtonElement>('button')!.click();
		await vi.waitFor(() => expect(host.querySelector('[role="status"]')!.textContent).toContain('1 comment not delivered'));
		flushSync();

		const buttons = [...host.querySelectorAll<HTMLButtonElement>('button')];
		expect(buttons.map((button) => button.textContent!.trim())).toEqual(['Retry']);
		expect(buttons[0].getAttribute('aria-label')).toMatch(/^Retry sending\. 1 comment not delivered/);
		// The summary is announced, never laid out in the bar.
		const summary = [...host.querySelectorAll('[role="status"]')].find((node) => node.textContent!.includes('not delivered'))!;
		expect(summary.closest('.sr-only, .undelivered.idle, .compact-summary')).not.toBeNull();
		fetchMock.mockResolvedValueOnce(result());
		buttons[0].click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
	});

	it('puts Send back in place of Sent when another comment is made', async () => {
		waitToSend();
		flushSync();
		host.querySelector<HTMLButtonElement>('button')!.click();
		await vi.waitFor(() => expect(host.querySelector('.sent')).not.toBeNull());

		waitToSend();
		flushSync();

		expect(host.querySelector('.sent')).toBeNull();
		expect(host.textContent).toContain('Send 1 comment');
		expect(host.querySelectorAll('.sending-now')).toHaveLength(1);
	});

	it('shows an undelivered reason and clears it after Retry', async () => {
		fetchMock.mockResolvedValueOnce(result([{ id: 'batch-1', item_count: 3, undelivered_reason: 'owner_unavailable', attempts: 1 }]));
		fetchMock.mockResolvedValueOnce(result());
		waitToSend();
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
		waitToSend();
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
			undelivered_batches: [{ id: 'batch-1', item_count: 3, undelivered_reason: 'owner_unavailable', attempts: 1 }],
		});
		flushSync();

		expect(host.textContent).toContain('Send 4 comments to Documents & Artifacts');
		expect(host.textContent).toContain('3 comments not delivered: its owner is unavailable');
		component!.sync('artifact-1', { open_count: 0, owner_label: null, undelivered_batches: [] });
	});

	it('offers Send with no countdown beside it', async () => {
		component!.sync('artifact-1', {
			open_count: 2,
			owner_label: 'Documents & Artifacts',
			undelivered_batches: [],
		});
		flushSync();

		expect(host.textContent).toContain('Send 2 comments to Documents & Artifacts');
		expect(host.querySelector('[role="status"].sending-grace')).toBeNull();
		host.querySelector<HTMLButtonElement>('button')!.click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/doc-feedback/send', expect.anything()));
	});

	it('keeps pending comments visible and shows a toast when Send fails', async () => {
		fetchMock.mockRejectedValue(new Error('offline'));
		component!.sync('artifact-1', { open_count: 1, owner_label: null, undelivered_batches: [] });
		flushSync();
		host.querySelector<HTMLButtonElement>('button')!.click();

		await vi.waitFor(() => expect(get(toasts).map((toast) => toast.message)).toContain("Couldn't send your comments — try again"));
		expect(host.textContent).toContain('Send 1 comment');
		component!.sync('artifact-1', { open_count: 0, owner_label: null, undelivered_batches: [] });
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
			undelivered_batches: [
				{ id: 'batch-1', item_count: 1, undelivered_reason: 'owner_unavailable', attempts: 1 },
				{ id: 'batch-2', item_count: 2, undelivered_reason: 'no_owner', attempts: 1 },
			],
		});
		flushSync();

		expect(host.textContent).toContain('3 comments not delivered');
		expect(host.textContent).not.toContain('not delivered:');
	});

	it('does not report a return when fresh state reaches a visible document', async () => {
		component!.sync('artifact-1', { open_count: 1, owner_label: null, undelivered_batches: [] });
		await Promise.resolve();

		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('does not report a return when a hidden tab refreshes its comments', async () => {
		setVisibility('hidden');
		component!.sync('artifact-1', { open_count: 1, owner_label: null, undelivered_batches: [] });
		await Promise.resolve();

		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('re-syncs through its existing owner callback when the page becomes visible', () => {
		Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
		window.dispatchEvent(new Event('visibilitychange'));

		expect(onFeedbackChanged).toHaveBeenCalledOnce();
	});
});
