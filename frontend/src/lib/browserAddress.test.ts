import { describe, expect, it } from 'vitest';
import { browserAddressLabel, browserTabLabel } from './browserAddress';

describe('browser pane labels', () => {
	it('leads with the backend-named registrable domain while eliding the middle', () => {
		expect(browserAddressLabel('https://accounts.example.co.uk/a/very/long/page?query=yes', 'example.co.uk')).toBe('example.co.uk · accounts · …page?query=yes');
	});

	it('leads with the real site of a lookalike host, never the name it imitates', () => {
		expect(browserAddressLabel('https://paypal.com.evil.com.br/login', 'evil.com.br')).toBe('evil.com.br · paypal.com · …login');
	});

	it('leads with the whole host when the domain is missing or names another site', () => {
		expect(browserAddressLabel('https://www.example.test/path')).toBe('www.example.test · …path');
		expect(browserAddressLabel('https://www.example.test/path', 'other.test')).toBe('www.example.test · …path');
	});

	it('keeps an IP address or single-label host whole', () => {
		expect(browserAddressLabel('http://127.0.0.1:5320/browser-pane-probe.html', '127.0.0.1')).toBe('127.0.0.1 · …browser-pane-probe.html');
		expect(browserAddressLabel('http://[::1]:3000/', '::1')).toBe('[::1]');
		expect(browserAddressLabel('http://localhost:3000/app', 'localhost')).toBe('localhost · …app');
	});

	it('shows a URL with no host as written', () => {
		expect(browserAddressLabel('about:blank')).toBe('about:blank');
	});

	it('uses the page title, falling back to the registrable domain', () => {
		expect(browserTabLabel('Welcome', 'https://www.example.test/path', 'example.test')).toBe('Welcome');
		expect(browserTabLabel('', 'https://www.example.test/path', 'example.test')).toBe('example.test · www · …path');
	});
});
