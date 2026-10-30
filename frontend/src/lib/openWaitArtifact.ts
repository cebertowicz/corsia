import { fetchArtifactPane } from '$lib/api/docs';
import type { ArtifactNode, PaneNode } from '$lib/api/artifactPane';
import { openArtifactAndShowThread } from '$lib/openArtifactInThread';

/** What a wait's label says once its document would not open: the state and the person's next step (C9). */
export const UNOPENABLE_DOCUMENT = 'Couldn’t open this document. Ask the agent in chat for a working link.';

/**
 * Opens the artifact an outside wait watches (EW7, EW8). The app opens a document by its path, so the id is
 * looked up in the pane of the thread that armed the watch, the same row an ask's tile opens from.
 * Resolves false when the pane read fails or the pane does not hold it, so the caller can say so.
 */
export async function openWaitArtifact(artifactId: string, title: string, threadId: string): Promise<boolean> {
	let nodes: PaneNode[];
	try {
		nodes = (await fetchArtifactPane(threadId)).nodes;
	} catch (error) {
		console.warn('openWaitArtifact: artifact pane read failed', { threadId, artifactId, error });
		return false;
	}
	const found = nodes
		.flatMap((node): ArtifactNode[] => (node.kind === 'artifact' ? [node] : node.children))
		.find((node) => node.id === artifactId);
	const path = found?.path ?? found?.external_id;
	if (!found || !path) {
		console.warn('openWaitArtifact: artifact not in the thread pane', { threadId, artifactId });
		return false;
	}
	openArtifactAndShowThread({ artifactId, path, title: found.title ?? title }, threadId);
	return true;
}
