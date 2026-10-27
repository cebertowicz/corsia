import { startArtifactRevisionWatch } from '$lib/artifactRevisionWatch';
import { startDocPresence } from '$lib/docPresenceRuntime';
import { startDeliveryPreferenceSync } from '$lib/commentDeliveryPreference';
import { startLiveDocuments } from '$lib/stores/liveDocuments';

/** The app shell's single entry for open-document freshness and presence. */
export { checkThreadRevisions } from '$lib/artifactRevisionWatch';

/** Starts the revision watch, live presence and the delivery-preference sync together; returns one stop. */
export function startDocumentRealtime(): () => void {
	const stopRevisions = startArtifactRevisionWatch();
	const stopPresence = startDocPresence();
	const stopLive = startLiveDocuments();
	const stopPreference = startDeliveryPreferenceSync();
	return () => {
		stopPreference();
		stopLive();
		stopPresence();
		stopRevisions();
	};
}
