import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { THREAD_SENT_EVENT } from '$lib/heldDocFeedbackScope';
import HeldDocFeedbackControl from './HeldDocFeedbackControl.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let held: { open_count: number; artifacts: { artifact_id: string; open_count: number }[] };
const fetchMock = vi.fn();

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
const settle = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); flushSync(); } };
const heldOf = (...rows: [string, number][]) => ({
	open_count: rows.reduce((sum, [, count]) => sum + count, 0),
	artifacts: rows.map(([artifact_id, open_count]) => ({ artifact_id, open_count })),
});

async function render(props: { threadId?: string; visibleArtifactId?: string | null } = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(HeldDocFeedbackControl, { target: host, props: { threadId: 'thread-1', ...props } }) as Record<string, unknown>;
	await settle();
}
const button = () => host.querySelector<HTMLButtonElement>('button');

beforeEach(() => {
	held = heldOf();
	fetchMock.mockReset();
	fetchMock.mockImplementation((url: string, init?: RequestInit) => {
		if (String(url).endsWith('/thread/send')) {
			const sent = held.open_count;
			held = heldOf();
			return json({ ...held, sent_count: sent });
		}
		void init;
		return json(held);
	});
	vi.stubGlobal('fetch', fetchMock);
	token.set('test-token');
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	token.set(null);
});

describe('HeldDocFeedbackControl', () => {
	it('shows nothing when the thread holds no document comments', async () => {
		await render();
		expect(button()).toBeNull();
	});

	it('offers Send for one held document that is not on screen', async () => {
		held = heldOf(['doc-a', 2]);
		await render({ visibleArtifactId: 'doc-b' });
		expect(button()!.textContent!.trim()).toBe('Send 2 comments');
	});

	it('leaves a held document that is on screen to its own header', async () => {
		held = heldOf(['doc-a', 2]);
		await render({ visibleArtifactId: 'doc-a' });
		expect(button()).toBeNull();
	});

	it('says all for several held documents, and a count of 101 stays a count', async () => {
		held = heldOf(['doc-a', 100], ['doc-b', 1]);
		await render({ visibleArtifactId: 'doc-a' });
		expect(button()!.textContent!.trim()).toBe('Send all 101 comments');
	});

	it('sends the thread\'s held document comments, tells open documents, and goes', async () => {
		held = heldOf(['doc-a', 2], ['doc-b', 1]);
		await render();
		const told = vi.fn();
		window.addEventListener(THREAD_SENT_EVENT, told);
		button()!.click();
		await settle();
		window.removeEventListener(THREAD_SENT_EVENT, told);

		const post = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/thread/send'))!;
		expect(JSON.parse(post[1].body)).toEqual({ thread_id: 'thread-1' });
		expect(told).toHaveBeenCalledOnce();
		expect(get(toasts).map((toast) => toast.message)).not.toContain('3 comments sent');
		expect(button()).toBeNull();
	});

	it('a failed send keeps the control and says so', async () => {
		held = heldOf(['doc-a', 2]);
		await render();
		fetchMock.mockImplementation((url: string) => (String(url).endsWith('/thread/send') ? Promise.reject(new Error('offline')) : json(held)));
		button()!.click();
		await settle();
		expect(get(toasts).map((toast) => toast.message)).toContain("Couldn't send your comments — try again");
		expect(button()!.textContent!.trim()).toBe('Send 2 comments');
	});

	it('a reading that is not a held-comments body shows nothing and breaks nothing', async () => {
		fetchMock.mockImplementation(() => json({ thread_kind: 'chat' }));
		await render();
		expect(button()).toBeNull();
	});

	it('reads again when a document\'s comments change', async () => {
		await render();
		expect(button()).toBeNull();
		held = heldOf(['doc-a', 1]);
		window.dispatchEvent(new Event('doc-feedback-changed'));
		await settle();
		expect(button()!.textContent!.trim()).toBe('Send 1 comment');
	});
});
