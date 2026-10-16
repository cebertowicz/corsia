import { get, writable } from 'svelte/store';
import { activeThreadId } from '$lib/stores/threads';
import { docPickerTarget } from '$lib/stores/docPickerTarget';
import { settingsHomeThread } from '$lib/stores/settingsNavigation';
import { openArtifactTab, type OpenArtifactInput } from '$lib/stores/artifactWorkspace';

/**
 * Set whenever an artifact was opened from a surface that is not the chat
 * canvas (the Documents library, Settings). The shell watches this and shows
 * the thread that now owns the tab, so the newly opened tab is actually on
 * screen. A fresh object each time, so opening twice in a row still fires.
 */
export const showArtifactThreadRequest = writable<{ threadId: string } | null>(null);

function homeThread(threadId?: string | null): string | null {
	return threadId ?? get(docPickerTarget)?.threadId ?? get(activeThreadId) ?? get(settingsHomeThread);
}

/**
 * Opens a document as a tab in a known thread.
 *
 * Every editable/commentable artifact needs a home thread, because that is
 * where its comments and edits are routed. The thread is, in order: the one the
 * caller names, the thread a Documents-library visit is picking *for*, the
 * currently active thread, or the thread Settings was opened over. With none of
 * those there is no home, so the caller must present the document as
 * unopenable rather than opening an orphan editor.
 *
 * Returns the thread the tab was opened in, or null when there is none.
 */
export function openArtifactInThread(
	input: OpenArtifactInput,
	threadId?: string | null,
): string | null {
	const picker = get(docPickerTarget);
	const target = homeThread(threadId);
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
	if (target) showArtifactThreadRequest.set({ threadId: target });
	return target;
}

/** Whether a document can be opened right now — false with no home thread. */
export function canOpenArtifact(threadId?: string | null): boolean {
	return !!homeThread(threadId);
}
