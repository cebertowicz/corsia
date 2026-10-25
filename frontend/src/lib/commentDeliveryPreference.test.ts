import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	deliverCommentsImmediately,
	documentCommentDeliveryPolicy,
	setDeliverCommentsImmediately,
} from './commentDeliveryPreference';

const KEY = 'corsia.document-comments.deliver-immediately';

afterEach(() => {
	localStorage.removeItem(KEY);
	deliverCommentsImmediately.set(true);
});

describe('comment delivery preference', () => {
	it('asks for the lane the toggle shows', () => {
		setDeliverCommentsImmediately(false);
		expect(get(deliverCommentsImmediately)).toBe(false);
		expect(documentCommentDeliveryPolicy()).toBe('document-held');

		setDeliverCommentsImmediately(true);
		expect(documentCommentDeliveryPolicy()).toBe('immediate');
	});

	it('follows a change made in another tab', () => {
		localStorage.setItem(KEY, 'false');
		window.dispatchEvent(new StorageEvent('storage', { key: KEY, newValue: 'false' }));
		expect(get(deliverCommentsImmediately)).toBe(false);
		expect(documentCommentDeliveryPolicy()).toBe('document-held');

		localStorage.setItem(KEY, 'true');
		window.dispatchEvent(new StorageEvent('storage', { key: KEY, newValue: 'true' }));
		expect(documentCommentDeliveryPolicy()).toBe('immediate');
	});

	it('ignores another key changing', () => {
		setDeliverCommentsImmediately(false);
		window.dispatchEvent(new StorageEvent('storage', { key: 'unrelated', newValue: 'x' }));
		expect(get(deliverCommentsImmediately)).toBe(false);
	});
});
