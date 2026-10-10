import type { BarPoint } from './measurementBarChart';
import { periodChange, windowPair } from './measurementPeriodView';
import { periodName, type MeasurementPeriod } from '$lib/stores/measurementPeriod';
import type { CostPeriod, CostWindowFigures } from '$lib/stores/measurementCost';

/** One comparison window of the all-in cost per hour of delivered work; `perHour` is the served rate, withheld by the server under the minimum n. */
export interface CostWindow { dollars: number | null; hours: number; sized: number; perHour: number | null }

/** The all-in cost per hour a chart draws: the served rate, or for a period under the minimum n the same spend over hours, drawn hollow. Unknown stays null, never zero. */
const drawnPerHour = (w: CostPeriod): number | null => w.per_hour ?? (w.sized > 0 && w.hours > 0 && w.dollars !== null ? w.dollars / w.hours : null);

/** One bar per served period, and the moving average the server served for it; a period without one draws no point on the line. */
export function costBarPoints(series: CostPeriod[]): BarPoint[] {
	return series.map((w) => ({ start: w.period, value: drawnPerHour(w), n: w.sized, ma: w.average }));
}

export interface CostView { current: CostWindow; previous: CostWindow | null; nowText: string; prevText: string; change: string }

/** A pooled window as the backend serves it: its own periods, summed, and the rate it served for them. */
const served = (window: CostWindowFigures): CostWindow => ({ dollars: window.dollars, hours: window.hours, sized: window.sized, perHour: window.per_hour });

/**
 * The grain's two comparison windows exactly as the cost payload serves them; the client never picks a window of its own. The change
 * is the one every headline prints (`periodChange`): a previous window the served series covers only in part reads new.
 */
export function costView(data: { series: CostPeriod[]; latest: CostWindowFigures | null; previous: CostWindowFigures | null; period: MeasurementPeriod }): CostView | null {
	if (!data.latest || !data.series.length) return null;
	const current = served(data.latest), previous = data.previous ? served(data.previous) : null;
	const figure = (w: CostWindow | null) => ({ value: w?.perHour ?? null, n: w?.sized ?? 0 });
	const pair = windowPair(data.series.map((w) => w.period), data.period, { latest: figure(current), previous: figure(previous) });
	const { latest, previous: before } = data.period;
	return {
		current, previous, change: periodChange(pair, true),
		nowText: periodName(latest), prevText: previous ? periodName(before) : '',
	};
}
