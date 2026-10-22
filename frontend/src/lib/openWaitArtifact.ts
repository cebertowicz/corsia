import { fetchArtifactPane } from '$lib/api/docs';
import type { ArtifactNode } from '$lib/api/artifactPane';
import { openArtifactAndShowThread } from '$lib/openArtifactInThread';

/**
 * Opens the artifact an outside wait watches (EW7, EW8). The app opens a document by its path, so the id is
 * looked up in the pane of the thread that armed the watch, the same row an ask's tile opens from.
 * Nothing opens when the thread's pane does not hold it.
 */
export async function openWaitArtifact(artifactId: string, title: string, threadId?: string | null): Promise<void> {
	if (!threadId) return;
	try {
		const pane = await fetchArtifactPane(threadId);
		const found = pane.nodes
			.flatMap((node): ArtifactNode[] => (node.kind === 'artifact' ? [node] : node.children))
			.find((node) => node.id === artifactId);
		const path = found?.path ?? found?.external_id;
		if (found && path) openArtifactAndShowThread({ artifactId, path, title: found.title ?? title }, threadId);
	} catch {
		// The wait row stays as it was; the document simply does not open.
	}
}
