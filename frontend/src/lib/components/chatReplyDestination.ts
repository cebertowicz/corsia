import { openArtifactAndShowThread } from '$lib/openArtifactInThread';
import type { AttentionDestination } from '$lib/api/attentionQueue';

/** Opens a reply's document in the thread that owns it (else this chat) and carries the one-time reply landing intent. */
export function openDocumentOnReplies(destination: AttentionDestination, activeThreadId: string | null, threadId?: string | null): boolean {
	if (!destination.path) return false;
	return openArtifactAndShowThread(
		{ artifactId: destination.id, path: destination.path, title: destination.title ?? destination.path.split('/').pop() ?? destination.path, landOn: 'new-replies' },
		threadId ?? activeThreadId,
	) !== null;
}

/**
 * A review that names its document opens it where the reader is, as the Documents list's Open does;
 * only a destination without a path falls back to the list.
 */
export function openDocumentDestination(
	destination: AttentionDestination, threadId: string | null | undefined, activeThreadId: string | null, showInList: (documentId: string) => void,
): void {
	if (destination.path) openArtifactAndShowThread({ artifactId: destination.id, path: destination.path, title: destination.title ?? destination.path }, threadId ?? activeThreadId);
	else showInList(destination.id);
}
