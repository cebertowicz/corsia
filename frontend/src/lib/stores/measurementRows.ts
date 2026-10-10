import type { MeasurementRow } from './measurement';

/** What a trend chart draws for a period: the served value, or for a low-n period whose value is withheld, the raw value as a hollow bar. Unknown stays null, never zero. */
export const drawnValue = (row: Pick<MeasurementRow, 'value' | 'raw_value' | 'n'>): number | null => row.value ?? (row.n > 0 ? row.raw_value ?? null : null);

/** Rows as the API served them, rejecting a moving average or raw value that is not a number or null; one that is absent reads as none, never as zero. */
export function decodeSeriesRows(rows: MeasurementRow[]): MeasurementRow[] {
	const bad = (value: unknown) => value !== undefined && value !== null && !(typeof value === 'number' && Number.isFinite(value));
	for (const row of rows) {
		if (bad(row.average)) throw new Error('measurement average shape');
		if (bad(row.raw_value)) throw new Error('measurement raw value shape');
	}
	return rows;
}
