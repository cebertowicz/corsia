import { describe, expect, it } from 'vitest';
import { decodePeriod, periodName } from './measurementPeriod';

const period = {
	grain: 'days', timezone: 'America/Los_Angeles', current: '2026-10-09',
	range: { from: '2026-09-25', to: '2026-10-08' }, latest: { from: '2026-10-02', to: '2026-10-08' }, previous: { from: '2026-09-25', to: '2026-10-01' },
};

describe('Measurement period', () => {
	it('names a window by its first and last local day, `to` being exclusive', () => {
		const decoded = decodePeriod(period);
		expect(periodName(decoded.latest)).toBe('Oct 2 to Oct 8');
		expect(periodName(decoded.previous)).toBe('Sep 25 to Oct 1');
		expect(periodName({ from: '2026-10-08', to: '2026-10-08' })).toBe('Oct 8');
	});

	it('rejects a response whose period is missing or malformed instead of defaulting it', () => {
		expect(() => decodePeriod(undefined)).toThrow('period shape');
		expect(() => decodePeriod({ ...period, grain: 'months' })).toThrow('period shape');
		expect(() => decodePeriod({ ...period, latest: { from: 'x', to: 'y' } })).toThrow('period.latest shape');
	});
});
