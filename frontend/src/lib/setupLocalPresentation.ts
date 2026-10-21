// Local presentation uses CORSIA’s managed download only.

export function measuredDownloadTime(seconds: number | null | undefined): string | undefined {
	if (seconds == null || seconds < 0) return undefined;
	if (seconds < 90) return 'about 1 min';
	if (seconds < 90 * 60) return `about ${Math.round(seconds / 60)} min`;
	return `about ${Math.round(seconds / 3600)} hr`;
}
