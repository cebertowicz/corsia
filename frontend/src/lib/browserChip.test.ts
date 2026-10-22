import { describe, expect, it } from 'vitest';

import { BROWSER_CHIP_NAME, browserChipLabel, lastBrowserChip, parseBrowserChip } from './browserChip';

describe('the browser chip the client reads', () => {
	it('says the host, or Shared browser when there is none, and names what a click does', () => {
		expect(browserChipLabel({ host: 'github.com' })).toBe('github.com');
		expect(browserChipLabel({ host: null })).toBe('Shared browser');
		expect(BROWSER_CHIP_NAME).toBe('Reopen the shared browser');
	});

	it('reads the server field and drops anything that is not one', () => {
		expect(parseBrowserChip({ host: 'github.com' })).toEqual({ host: 'github.com' });
		expect(parseBrowserChip({ host: null })).toEqual({ host: null });
		expect(parseBrowserChip({ host: '' })).toEqual({ host: null });
		for (const raw of [undefined, null, 'github.com', 3, { label: 'x' }, { host: 3 }]) {
			expect(parseBrowserChip(raw)).toBeNull();
		}
	});

	it('picks the last call that carries a chip, deciding nothing itself', () => {
		const chip = (host: string) => ({ name: 'browser_open', browser_chip: { host } });
		expect(lastBrowserChip([chip('a.example'), { name: 'bash' }, chip('b.example'), { name: 'bash' }])).toEqual({ host: 'b.example' });
		// A call without the field earns no chip, whatever it is: the server decides.
		expect(lastBrowserChip([{ name: 'browser_open', args: { show_window: true, url: 'https://a.example' } }])).toBeNull();
		expect(lastBrowserChip([])).toBeNull();
		expect(lastBrowserChip(undefined)).toBeNull();
		expect(lastBrowserChip([null, 'x'])).toBeNull();
	});
});
