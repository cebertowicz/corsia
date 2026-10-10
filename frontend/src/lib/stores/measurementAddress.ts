/** The address names the Measurement page itself, so a reload or a shared link reopens it, not Activity. */
export const MEASUREMENT_VIEW = 'measurement';

export function measurementViewRequested(): boolean {
	return typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('view') === MEASUREMENT_VIEW;
}

/** Leaving the page takes its own keys out of the address; every other key stays. */
export function clearMeasurementUrl(): void {
	if (typeof window === 'undefined') return;
	const params = new URLSearchParams(window.location.search);
	if (params.get('view') === MEASUREMENT_VIEW) params.delete('view');
	for (const key of ['project', 'agent', 'grain', 'range']) params.delete(key);
	const query = params.toString();
	window.history.replaceState(window.history.state, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
}
