import type { ArtifactTab } from '$lib/artifactTabs';
import type { DocViewerTarget } from '$lib/docViewerTarget';

export function targetForTab(t: ArtifactTab, projectId: string | null, threadId: string | null): DocViewerTarget {
	if (t.kind !== 'document') throw new Error('Only document tabs have DocViewer targets');
	return {
		instanceId: t.instanceId,
		path: t.path,
		name: t.title,
		projectId,
		threadId,
		artifactId: t.artifactId,
		landOn: t.landOn,
	};
}
