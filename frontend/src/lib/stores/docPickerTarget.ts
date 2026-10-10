import { writable } from 'svelte/store';

/**
 * The thread that a Documents-library visit is picking a document *for*.
 *
 * Deliberately separate from `contextAttachTarget`: that store means "attach a
 * context file to this scope" and its consumers filter to markdown/text and
 * dedupe against already-attached files. Opening an artifact tab has neither
 * restriction, so overloading it would either hide valid targets or make
 * non-context files look attachable.
 */
export interface DocPickerTarget {
	threadId: string;
}

export const docPickerTarget = writable<DocPickerTarget | null>(null);

/** Clears ownership whenever navigation leaves the Documents picker journey. */
export function clearDocPickerTarget(): void {
	docPickerTarget.set(null);
}
