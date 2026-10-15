import { describe, expect, it } from 'vitest';
import { browserAddressLabel, browserTabLabel, registrableDomain } from './browserAddress';

describe('browser pane labels', () => {
	it('keeps the registrable domain first while eliding the middle', () => {
		expect(registrableDomain('accounts.example.co.uk')).toBe('example.co.uk');
		expect(browserAddressLabel('https://accounts.example.co.uk/a/very/long/page?query=yes')).toBe('example.co.uk · accounts · …page?query=yes');
	});

	it('keeps an IP address or single-label host whole', () => {
		expect(browserAddressLabel('http://127.0.0.1:5320/browser-pane-probe.html')).toBe('127.0.0.1 · …browser-pane-probe.html');
		expect(browserAddressLabel('http://[::1]:3000/')).toBe('[::1]');
		expect(browserAddressLabel('http://localhost:3000/app')).toBe('localhost · …app');
	});

	it('uses the page title, falling back to the registrable domain', () => {
		expect(browserTabLabel('Welcome', 'https://www.example.test/path')).toBe('Welcome');
		expect(browserTabLabel('', 'https://www.example.test/path')).toBe('example.test · www · …path');
	});
});
