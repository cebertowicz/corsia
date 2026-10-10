import { startArtifactRevisionWatch } from '$lib/artifactRevisionWatch';
import { startDocPresence } from '$lib/docPresenceRuntime';
import { startLiveDocuments } from '$lib/stores/liveDocuments';

/** The app shell's single entry for open-document freshness and presence. */
export { checkThreadRevisions } from '$lib/artifactRevisionWatch';

/** Starts the revision watch and live presence together; returns one stop. */
export function startDocumentRealtime(): () => void {
	const stopRevisions = startArtifactRevisionWatch();
	const stopPresence = startDocPresence();
	const stopLive = startLiveDocuments();
	return () => {
		stopLive();
		stopPresence();
		stopRevisions();
	};
}
