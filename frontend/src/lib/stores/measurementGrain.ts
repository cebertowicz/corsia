import { get, writable } from 'svelte/store';
import { token } from './auth';

/** The page's one time grain: each bar, tile and comparison is a calendar day or a Monday to Sunday week. */
export type MeasurementGrain = 'days' | 'weeks';
export type MeasurementScopeKey = { project: string; agent: string | null };

export const DEFAULT_GRAIN: MeasurementGrain = 'days';

export const measurementGrain = writable<MeasurementGrain>(DEFAULT_GRAIN);

/** A grain read from the address; anything unknown is the default. */
export function parseGrain(value: string | null): MeasurementGrain {
	return value === 'weeks' ? 'weeks' : DEFAULT_GRAIN;
}

/** The viewer's IANA zone, so days and weeks start at the viewer's midnight. */
export function viewerTimeZone(): string {
	return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** The query every grain-aware Measurement request carries: scope, grain and the viewer's zone. */
export function grainScopeParams(grain: MeasurementGrain, scope: MeasurementScopeKey): URLSearchParams {
	const params = new URLSearchParams({ project: scope.project, agent: scope.agent ?? 'none', grain, timezone: viewerTimeZone() });
	const currentToken = get(token);
	if (currentToken) params.set('token', currentToken);
	return params;
}
