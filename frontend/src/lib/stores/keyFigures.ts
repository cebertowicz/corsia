import { get, writable } from 'svelte/store';
import { measurementScope, type MeasurementScope } from './measurement';
import { grainScopeParams, measurementGrain, viewerTimeZone, type MeasurementGrain } from './measurementGrain';
import { decodeSpan } from './measurementPeriod';

/** A token figure: the sum of what was recorded and how many calls recorded none of it (a lower bound when above zero). */
export type KpiTokenFigure = { value: number; unknown_records: number };
export type KpiPeriod = {
	start: string; end: string;
	/** The local dates the period covers, both inclusive. */
	from: string; to: string;
	tokens: { total: KpiTokenFigure; input: KpiTokenFigure; cache_write: KpiTokenFigure; cache_read: KpiTokenFigure; output: KpiTokenFigure };
};
export type Kpi = { current: KpiPeriod; previous: KpiPeriod };
export type KeyFiguresState = { loading: boolean; kpi: Kpi | null; error: string | null; selection: string };

export const keyFigures = writable<KeyFiguresState>({ loading: false, kpi: null, error: null, selection: '' });
let controller: AbortController | undefined;

function key(scope: MeasurementScope, timezone: string, grain: MeasurementGrain): string { return `${scope.project}:${scope.agent ?? 'none'}:${timezone}:${grain}`; }
export async function loadKeyFigures(scope = get(measurementScope), grain = get(measurementGrain)): Promise<void> {
	controller?.abort();
	controller = new AbortController();
	const active = controller;
	const timezone = viewerTimeZone();
	const selection = key(scope, timezone, grain);
	keyFigures.update((state) => ({ loading: true, kpi: state.selection === selection ? state.kpi : null, error: null, selection }));
	try {
		const params = grainScopeParams(grain, scope);
		const response = await fetch(`/api/measurement/key-figures?${params}`, { signal: active.signal });
		if (!response.ok) throw new Error('key figures');
		const data = await response.json() as { kpi?: Kpi };
		if (!data.kpi?.current || !data.kpi.previous) throw new Error('key figures shape');
		// A period without the dates it covers is rejected by the one span decoder, never defaulted.
		decodeSpan(data.kpi.current, 'current'); decodeSpan(data.kpi.previous, 'previous');
		if (controller !== active || selection !== key(get(measurementScope), timezone, get(measurementGrain))) return;
		keyFigures.set({ loading: false, kpi: data.kpi, error: null, selection });
	} catch (error) {
		if ((error as DOMException).name === 'AbortError' || controller !== active) return;
		// A failed load shows no values at all, never the previous scope's or an old response's.
		keyFigures.set({ loading: false, kpi: null, error: "Couldn't load today's figures.", selection });
	}
}
