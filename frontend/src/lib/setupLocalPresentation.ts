// Local presentation uses CORSIA’s managed download only.

export const LOCAL_ONLY_MODEL_GUIDANCE = 'Local models are good for private work. For running many agents at once, cloud models are recommended.';

type LocalOnlyGuidanceInput = { eligible: boolean; localReady: boolean; signedInSubscription: boolean; hasApiKey: boolean };

/** The recommendation appears only when local is the available path forward. */
export function showLocalOnlyGuidance({ eligible, localReady, signedInSubscription, hasApiKey }: LocalOnlyGuidanceInput): boolean {
	return eligible && localReady && !signedInSubscription && !hasApiKey;
}

export function measuredDownloadTime(seconds: number | null | undefined): string | undefined {
	if (seconds == null || seconds < 0) return undefined;
	if (seconds < 90) return 'about 1 min';
	if (seconds < 90 * 60) return `about ${Math.round(seconds / 60)} min`;
	return `about ${Math.round(seconds / 3600)} hr`;
}
