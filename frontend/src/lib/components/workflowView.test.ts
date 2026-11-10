import { describe, expect, it } from 'vitest';
import { isWithheld } from './measurementBarChart';
import { chartPoints, type MeasureValue, type WorkflowEntry } from './workflowView';

const none: MeasureValue = { numerator: null, n: 0, value: null };
const recorded = (value: number, n: number): MeasureValue => ({ numerator: value * n, n, value });
const week = (start: string, runs: number, cost: MeasureValue) => ({
  start, version: 'v1', version_id: 'v1', runs, failed: 0,
  measures: { corrections: none, gate_rounds: none, first_try: none, cost, minutes: none, owner_measure: none },
});

describe('chartPoints', () => {
  it('counts the week\'s runs, so a week that ran with nothing recorded is withheld, not empty', () => {
    const entry = { weekly: [week('2026-10-05', 3, recorded(2, 1)), week('2026-10-12', 2, none)] } as unknown as WorkflowEntry;
    const [partial, unrecorded, ...rest] = chartPoints(entry, 'v1', 'cost');
    expect(partial).toMatchObject({ n: 3, value: 2 });
    expect(unrecorded).toMatchObject({ n: 2, value: null });
    expect(isWithheld(unrecorded)).toBe(true);
    expect(rest).toEqual([]);
  });

  it('leaves a week without runs of the version empty', () => {
    const entry = { weekly: [week('2026-10-05', 1, recorded(1, 1)), { ...week('2026-10-12', 4, recorded(1, 4)), version_id: 'v2' }] } as unknown as WorkflowEntry;
    const points = chartPoints(entry, 'v1', 'cost');
    expect(points[1]).toMatchObject({ start: '2026-10-12', n: 0, value: null });
    expect(isWithheld(points[1])).toBe(false);
  });
});
