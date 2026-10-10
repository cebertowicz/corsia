import { get, writable } from 'svelte/store';
import { activeThreadId } from '$lib/stores/threads';
import { docPickerTarget } from '$lib/stores/docPickerTarget';
import { openArtifactTab, type OpenArtifactInput } from '$lib/stores/artifactWorkspace';

/**
 * Bumped whenever an artifact was opened from a surface that is not the chat
 * canvas (the Documents library, a settings takeover). The shell watches this
 * and returns to the owning thread so the newly opened tab is actually on
 * screen — the same counter-signal convention as `settingsOpenRequest`.
 */
export const showArtifactThreadRequest = writable(0);

/**
 * Opens a document as a tab in a known thread.
 *
 * Every editable/commentable artifact needs a home thread, because that is
 * where its comments and edits are routed. The thread is, in order: the one the
 * caller names, the thread a Documents-library visit is picking *for*, or the
 * currently active thread. With none of those there is no home, so the caller
 * must present the document as unopenable rather than opening an orphan editor.
 *
 * Returns the thread the tab was opened in, or null when there is none.
 */
export function openArtifactInThread(
	input: OpenArtifactInput,
	threadId?: string | null,
): string | null {
	const picker = get(docPickerTarget);
	const target = threadId ?? picker?.threadId ?? get(activeThreadId);
	if (!target) return null;
	openArtifactTab(target, input);
	if (picker) docPickerTarget.set(null);
	return target;
}

/** Opens the document and asks the shell to bring its thread into view. */
export function openArtifactAndShowThread(
	input: OpenArtifactInput,
	threadId?: string | null,
): string | null {
	const target = openArtifactInThread(input, threadId);
	if (target) showArtifactThreadRequest.update((n) => n + 1);
	return target;
}

/** Whether a document can be opened right now — false with no home thread. */
export function canOpenArtifact(threadId?: string | null): boolean {
	return !!(threadId ?? get(docPickerTarget)?.threadId ?? get(activeThreadId));
}
