import { describe, expect, it } from 'vitest';
import { measuredDownloadTime } from './setupLocalPresentation';

describe('measured download time', () => {
	it('says nothing until a time is measured', () => {
		expect(measuredDownloadTime(null)).toBeUndefined();
		expect(measuredDownloadTime(undefined)).toBeUndefined();
		expect(measuredDownloadTime(-1)).toBeUndefined();
	});

	it('rounds to minutes, then hours', () => {
		expect(measuredDownloadTime(30)).toBe('about 1 min');
		expect(measuredDownloadTime(12 * 60)).toBe('about 12 min');
		expect(measuredDownloadTime(3 * 3600)).toBe('about 3 hr');
	});
});
