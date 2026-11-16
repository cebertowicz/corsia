import { describe, expect, it } from 'vitest';

import { BROWSER_CHIP_NAME, browserChipAccessibleName, browserChipLabel, parseBrowserChip } from './browserChip';

describe('the browser chip the client reads', () => {
	it('leads with the registrable domain, then the subdomain, as the pane address does', () => {
		expect(browserChipLabel({ host: 'accounts.example.com.other-site.net', domain: 'other-site.net' }))
			.toBe('other-site.net · accounts.example.com');
	});

	it('reads a host that is its own domain unchanged', () => {
		expect(browserChipLabel({ host: 'github.com', domain: 'github.com' })).toBe('github.com');
	});

	it('shows the whole host when the chip carries no domain', () => {
		expect(browserChipLabel({ host: 'www.example.co.uk', domain: null })).toBe('www.example.co.uk');
		expect(browserChipLabel({ host: '127.0.0.1' })).toBe('127.0.0.1');
	});

	it('says the host, or Shared browser when there is none, and names what a click does', () => {
		expect(browserChipLabel({ host: 'github.com' })).toBe('github.com');
		expect(browserChipLabel({ host: null })).toBe('Shared browser');
		expect(BROWSER_CHIP_NAME).toBe('Reopen the shared browser');
	});

	it('names itself by what it says, then what a click does', () => {
		expect(browserChipAccessibleName({ host: 'github.com' })).toBe('github.com, reopen the shared browser');
		expect(browserChipAccessibleName({ host: 'accounts.example.com.other-site.net', domain: 'other-site.net' }))
			.toBe('other-site.net · accounts.example.com, reopen the shared browser');
		expect(browserChipAccessibleName({ host: null })).toBe('Reopen the shared browser');
	});

	it('reads the server field and drops anything that is not one', () => {
		expect(parseBrowserChip({ host: 'github.com', domain: 'github.com' })).toEqual({ host: 'github.com', domain: 'github.com' });
		expect(parseBrowserChip({ host: null })).toEqual({ host: null, domain: null });
		expect(parseBrowserChip({ host: '' })).toEqual({ host: null, domain: null });
		for (const raw of [undefined, null, 'github.com', 3, { label: 'x' }, { host: 3 }, { host: 'x', domain: 3 }]) {
			expect(parseBrowserChip(raw)).toBeNull();
		}
	});

	it('still parses a chip stored before the domain field existed', () => {
		expect(parseBrowserChip({ host: 'accounts.example.com.other-site.net' })).toEqual({
			host: 'accounts.example.com.other-site.net', domain: null,
		});
	});
});
