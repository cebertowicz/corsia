import type { MeasurementGrain } from './measurementGrain';

/** A span of local dates as the backend serves it: `from` first day, `to` last day, both inclusive. */
export type PeriodSpan = { from: string; to: string };
/** The periods a response answered for, echoed by every grain-aware Measurement route. */
export type MeasurementPeriod = {
	grain: MeasurementGrain; timezone: string; current: string;
	range: PeriodSpan; latest: PeriodSpan; previous: PeriodSpan;
};

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Read one served span of local dates; one without both ISO dates is rejected, never defaulted. */
export function decodeSpan(value: unknown, name: string): PeriodSpan {
	const item = value as Record<string, unknown> | null;
	if (!item || typeof item.from !== 'string' || typeof item.to !== 'string' || !ISO_DATE.test(item.from) || !ISO_DATE.test(item.to)) throw new Error(`period.${name} shape`);
	return { from: item.from, to: item.to };
}

/** Read the echoed period; a response without a valid one is rejected, never defaulted. */
export function decodePeriod(value: unknown): MeasurementPeriod {
	const item = value as Record<string, unknown> | null;
	if (!item || (item.grain !== 'days' && item.grain !== 'weeks') || typeof item.timezone !== 'string' || typeof item.current !== 'string') throw new Error('period shape');
	return { grain: item.grain, timezone: item.timezone, current: item.current, range: decodeSpan(item.range, 'range'), latest: decodeSpan(item.latest, 'latest'), previous: decodeSpan(item.previous, 'previous') };
}

/** How many periods the served moving average spans (the server's comparison window); absent stays unknown, a malformed one rejects the response. */
export function decodeAveragePeriods(value: unknown): number | undefined {
	if (value === undefined) return undefined;
	if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) throw new Error('average_periods shape');
	return value;
}

function shortDate(iso: string): string {
	const [, month, day] = iso.split('-').map(Number);
	return `${MONTHS[month - 1]} ${day}`;
}

/** A window named by its dates: "Sep 28 to Oct 4", or "Oct 8" for one day. */
export function periodName(window: PeriodSpan): string {
	return window.to === window.from ? shortDate(window.from) : `${shortDate(window.from)} to ${shortDate(window.to)}`;
}
