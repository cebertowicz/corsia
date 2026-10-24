import { get, writable } from 'svelte/store';

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

// Another tab changing the preference changes this tab's toggle, and so what its comments, edits and reopens request.
if (typeof window !== 'undefined') {
	window.addEventListener('storage', (event) => {
		if (event.key === KEY || event.key === null) deliverCommentsImmediately.set(initialValue());
	});
}

/** The lane a person's comment asks for: the same value the toggle shows. */
export function documentCommentDeliveryPolicy(): 'immediate' | 'document-held' {
	return get(deliverCommentsImmediately) ? 'immediate' : 'document-held';
}
