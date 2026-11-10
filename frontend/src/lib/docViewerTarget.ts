import type { DocumentLanding } from '$lib/artifactTabTypes';

/**
 * One editor session: the tab instance that owns it, plus where to load and
 * save. `instanceId` is the draft/save-queue key, so a document that resolves
 * an artifact id or moves on disk keeps the same unsaved text and pending save.
 */
export interface DocViewerTarget {
	instanceId: string;
	path: string;
	name: string;
	projectId: string | null;
	threadId: string | null;
	artifactId: string | null;
	/** Tab-scoped arrival instruction, cleared after the viewer uses it. */
	landOn?: DocumentLanding;
}
