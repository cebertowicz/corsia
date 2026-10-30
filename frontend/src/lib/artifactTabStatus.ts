import type { ArtifactTab } from './artifactTabTypes';

/** A newer content revision exists than the one this tab last loaded. */
export function isTabChanged(tab: ArtifactTab): boolean {
	if (tab.kind !== 'document') return false;
	if (tab.loadedRevision === null || tab.latestRevision === null) return false;
	return tab.loadedRevision !== tab.latestRevision;
}
