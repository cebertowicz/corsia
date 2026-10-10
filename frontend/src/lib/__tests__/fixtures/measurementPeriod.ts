import type { MeasurementPeriod } from '$lib/stores/measurementPeriod';

const DAY_MS = 86_400_000;
const shift = (iso: string, days: number) => new Date(Date.parse(`${iso}T00:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10);

/** The echoed period of a Weeks response whose last completed week starts on `lastWeek`: 4 weeks against the 4 before, both ends inclusive. */
export function periodsPeriod(lastPeriod: string): MeasurementPeriod {
	const latestFrom = shift(lastPeriod, -21);
	return {
		grain: 'weeks', timezone: 'America/Los_Angeles', current: shift(lastPeriod, 7),
		range: { from: shift(lastPeriod, -77), to: shift(lastPeriod, 6) },
		latest: { from: latestFrom, to: shift(lastPeriod, 6) },
		previous: { from: shift(latestFrom, -28), to: shift(latestFrom, -1) },
	};
}

/** The echoed period of a Days response whose last completed day is `lastDay`: 7 days against the 7 before. */
export function daysPeriod(lastDay: string): MeasurementPeriod {
	const latestFrom = shift(lastDay, -6);
	return {
		grain: 'days', timezone: 'America/Los_Angeles', current: shift(lastDay, 1),
		range: { from: shift(lastDay, -27), to: lastDay },
		latest: { from: latestFrom, to: lastDay },
		previous: { from: shift(latestFrom, -7), to: shift(latestFrom, -1) },
	};
}
