import { writable } from 'svelte/store';

const KEY = 'corsia.document-comments.deliver-immediately';

function initialValue(): boolean {
	if (typeof localStorage === 'undefined') return true;
	return localStorage.getItem(KEY) !== 'false';
}

export const deliverCommentsImmediately = writable(initialValue());

export function setDeliverCommentsImmediately(value: boolean): void {
	if (typeof localStorage !== 'undefined') localStorage.setItem(KEY, String(value));
	deliverCommentsImmediately.set(value);
}

export function documentCommentDeliveryPolicy(): 'immediate' | 'document-held' {
	return initialValue() ? 'immediate' : 'document-held';
}
