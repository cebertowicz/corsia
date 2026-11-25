import { findDocument } from '$lib/findDocument';
import { openArtifactAndShowThread } from '$lib/openArtifactInThread';

/** What a wait's label says once its document would not open: the state and the person's next step (C9). */
export const UNOPENABLE_DOCUMENT = 'Couldn’t open this document. Ask the agent in chat for a working link.';

/** Opens the artifact an outside wait watches, independent of its pane registration. */
export async function openWaitArtifact(artifactId: string, title: string, threadId: string): Promise<boolean> {
	const found = await findDocument(artifactId);
	if (found.status !== 'found') {
		console.warn('openWaitArtifact: artifact could not be resolved', { threadId, artifactId, status: found.status });
		return false;
	}
	openArtifactAndShowThread({ artifactId, path: found.path, title: found.title ?? title }, threadId);
	return true;
}
