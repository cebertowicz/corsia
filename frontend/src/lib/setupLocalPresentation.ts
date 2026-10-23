// Local presentation uses CORSIA’s managed download only.

export const LOCAL_ONLY_MODEL_GUIDANCE = 'Good for private work. For now, to run many agents at once, add a cloud model.';

type LocalOnlyGuidanceInput = { eligible: boolean; localInProgress: boolean; cloudReady: boolean };

/** The recommendation appears while local is the only working path forward. */
export function showLocalOnlyGuidance({ eligible, localInProgress, cloudReady }: LocalOnlyGuidanceInput): boolean {
	return eligible && localInProgress && !cloudReady;
}

export function measuredDownloadTime(seconds: number | null | undefined): string | undefined {
	if (seconds == null || seconds < 0) return undefined;
	if (seconds < 90) return 'about 1 min';
	if (seconds < 90 * 60) return `about ${Math.round(seconds / 60)} min`;
	return `about ${Math.round(seconds / 3600)} hr`;
}
