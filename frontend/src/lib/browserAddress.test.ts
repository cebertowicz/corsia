import { describe, expect, it } from 'vitest';
import { browserAddressLabel, browserTabLabel, registrableDomain } from './browserAddress';

describe('browser pane labels', () => {
	it('keeps the registrable domain first while eliding the middle', () => {
		expect(registrableDomain('accounts.example.co.uk')).toBe('example.co.uk');
		expect(browserAddressLabel('https://accounts.example.co.uk/a/very/long/page?query=yes')).toBe('example.co.uk · accounts · …page?query=yes');
	});

	it('uses the page title, falling back to the registrable domain', () => {
		expect(browserTabLabel('Welcome', 'https://www.example.test/path')).toBe('Welcome');
		expect(browserTabLabel('', 'https://www.example.test/path')).toBe('example.test · www · …path');
	});
});
