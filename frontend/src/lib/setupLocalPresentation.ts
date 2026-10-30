// Local presentation uses CORSIA’s managed download only.

export const LOCAL_ONLY_MODEL_GUIDANCE = 'Good for private work. To run many agents at once, add a cloud model.';

type LocalOnlyGuidanceInput = { eligible: boolean; localModelActive: boolean; cloudReady: boolean };

/** The recommendation appears while local is the only working path forward. */
export function showLocalOnlyGuidance({ eligible, localModelActive, cloudReady }: LocalOnlyGuidanceInput): boolean {
	return eligible && localModelActive && !cloudReady;
}

export function measuredDownloadTime(seconds: number | null | undefined): string | undefined {
	if (seconds == null || seconds < 0) return undefined;
	if (seconds < 60) return 'under 1 min left';
	if (seconds < 90) return '1 min left';
	if (seconds < 90 * 60) return `${Math.round(seconds / 60)} min left`;
	return `${Math.round(seconds / 3600)} hr left`;
}
