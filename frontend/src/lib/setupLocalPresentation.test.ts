import { describe, expect, it } from 'vitest';
import { measuredDownloadTime, showLocalOnlyGuidance } from './setupLocalPresentation';

describe('local-only guidance', () => {
	it('shows while a local model is active and no cloud model works', () => {
		expect(showLocalOnlyGuidance({ eligible: true, localModelActive: true, cloudReady: false })).toBe(true);
		expect(showLocalOnlyGuidance({ eligible: true, localModelActive: false, cloudReady: false })).toBe(false);
		expect(showLocalOnlyGuidance({ eligible: false, localModelActive: true, cloudReady: false })).toBe(false);
		expect(showLocalOnlyGuidance({ eligible: true, localModelActive: true, cloudReady: true })).toBe(false);
	});
});

describe('measured download time', () => {
	it('says nothing until a time is measured', () => {
		expect(measuredDownloadTime(null)).toBeUndefined();
		expect(measuredDownloadTime(undefined)).toBeUndefined();
		expect(measuredDownloadTime(-1)).toBeUndefined();
	});

	it('rounds to minutes, then hours', () => {
		expect(measuredDownloadTime(30)).toBe('Under 1 min left');
		expect(measuredDownloadTime(75)).toBe('1 min left');
		expect(measuredDownloadTime(12 * 60)).toBe('12 min left');
		expect(measuredDownloadTime(3 * 3600)).toBe('3 hr left');
	});
});
