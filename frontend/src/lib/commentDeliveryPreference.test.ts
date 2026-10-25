import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	deliverCommentsImmediately,
	documentCommentDeliveryPolicy,
	refreshDeliveryPreference,
	setDeliverCommentsImmediately,
	startDeliveryPreferenceSync,
} from './commentDeliveryPreference';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';

const KEY = 'corsia.document-comments.deliver-immediately';
const ROUTE = '/api/doc-feedback/delivery-preference';

let server: { immediate: boolean; stored: boolean };
let fetchMock: ReturnType<typeof vi.fn>;
const puts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT').map(([, init]) => JSON.parse(init.body));

function answer(status = 200) {
	fetchMock = vi.fn().mockImplementation((_url: string, init: { method: string; body?: string }) => {
		if (status !== 200) return Promise.resolve(new Response('{}', { status }));
		if (init.method === 'PUT') server = { immediate: JSON.parse(init.body!).immediate, stored: true };
		return Promise.resolve(new Response(JSON.stringify(server)));
	});
	vi.stubGlobal('fetch', fetchMock);
}

beforeEach(() => {
	server = { immediate: true, stored: false };
	token.set('test-token');
	answer();
});

afterEach(() => {
	localStorage.removeItem(KEY);
	deliverCommentsImmediately.set(true);
	token.set(null);
	vi.unstubAllGlobals();
});

describe('comment delivery preference', () => {
	it('asks for the lane the toggle shows and saves each choice on the server', async () => {
		expect(await setDeliverCommentsImmediately(false)).toBe(true);
		expect(get(deliverCommentsImmediately)).toBe(false);
		expect(documentCommentDeliveryPolicy()).toBe('document-held');

		await setDeliverCommentsImmediately(true);
		expect(documentCommentDeliveryPolicy()).toBe('immediate');
		expect(puts()).toEqual([{ immediate: false }, { immediate: true }]);
		expect(fetchMock.mock.calls[0][0]).toBe(ROUTE);
		expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer test-token');
	});

	it('puts the toggle back when the server does not keep the choice', async () => {
		answer(500);
		expect(await setDeliverCommentsImmediately(false)).toBe(false);
		expect(get(deliverCommentsImmediately)).toBe(true);
	});

	it('reads the person\'s stored choice', async () => {
		server = { immediate: false, stored: true };
		await refreshDeliveryPreference();
		expect(get(deliverCommentsImmediately)).toBe(false);
		expect(puts()).toEqual([]);
	});

	it('hands this browser\'s earlier "off" to the server once, when the server has none', async () => {
		localStorage.setItem(KEY, 'false');
		await refreshDeliveryPreference();
		expect(puts()).toEqual([{ immediate: false }]);
		expect(get(deliverCommentsImmediately)).toBe(false);
		expect(localStorage.getItem(KEY)).toBeNull();

		await refreshDeliveryPreference();
		expect(puts()).toHaveLength(1);
	});

	it('lets the server value win over an earlier browser value once the server has one', async () => {
		localStorage.setItem(KEY, 'false');
		server = { immediate: true, stored: true };
		await refreshDeliveryPreference();
		expect(puts()).toEqual([]);
		expect(get(deliverCommentsImmediately)).toBe(true);
		expect(localStorage.getItem(KEY)).toBeNull();
	});

	it('keeps the earlier browser value for another try when the server cannot be reached', async () => {
		localStorage.setItem(KEY, 'false');
		answer(503);
		await refreshDeliveryPreference();
		expect(localStorage.getItem(KEY)).toBe('false');
	});

	it('follows a change made on another client, and re-reads on connect', async () => {
		const stop = startDeliveryPreferenceSync();
		try {
			emit('doc_delivery_preference_changed', { immediate: false });
			expect(documentCommentDeliveryPolicy()).toBe('document-held');
			emit('doc_delivery_preference_changed', { immediate: 'x' });
			expect(documentCommentDeliveryPolicy()).toBe('document-held');
			emit('doc_delivery_preference_changed', { immediate: true });
			expect(documentCommentDeliveryPolicy()).toBe('immediate');

			server = { immediate: false, stored: true };
			connectionState.set('connected');
			await vi.waitFor(() => expect(documentCommentDeliveryPolicy()).toBe('document-held'));
		} finally {
			stop();
			connectionState.set('disconnected');
		}
	});
});
