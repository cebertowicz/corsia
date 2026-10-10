import { writable } from 'svelte/store';

/**
 * Suggestions whose Accept the server refused because the saved file no longer has the text they
 * replace (HTTP 409), per document: comment id to the server's message. This is a decision outcome,
 * not anchoring: the text on screen decides where every comment sits, and a refusal only says that
 * this card's Accept cannot work. It lasts until that suggestion is decided or the comment surface
 * resets (the document changes or its comments close); edits, saves and comment loads leave it.
 */
export const suggestionRefusals = writable<Record<string, Record<string, string>>>({});

export function recordRefusal(artifactId: string, commentId: string, message: string) {
	suggestionRefusals.update((all) => ({ ...all, [artifactId]: { ...all[artifactId], [commentId]: message } }));
}

export function clearRefusal(artifactId: string, commentId: string) {
	suggestionRefusals.update((all) => !all[artifactId]?.[commentId] ? all : {
		...all, [artifactId]: Object.fromEntries(Object.entries(all[artifactId]).filter(([id]) => id !== commentId)),
	});
}

export function clearRefusals(artifactId: string) {
	suggestionRefusals.update((all) => !all[artifactId] ? all : Object.fromEntries(Object.entries(all).filter(([id]) => id !== artifactId)));
}
