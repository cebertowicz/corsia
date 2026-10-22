import { describe, expect, it } from 'vitest';

import { BROWSER_CHIP_NAME, browserChipAccessibleName, browserChipLabel, parseBrowserChip } from './browserChip';

describe('the browser chip the client reads', () => {
	it('says the host, or Shared browser when there is none, and names what a click does', () => {
		expect(browserChipLabel({ host: 'github.com' })).toBe('github.com');
		expect(browserChipLabel({ host: null })).toBe('Shared browser');
		expect(BROWSER_CHIP_NAME).toBe('Reopen the shared browser');
	});

	it('names itself by what it says, then what a click does', () => {
		expect(browserChipAccessibleName({ host: 'github.com' })).toBe('github.com, reopen the shared browser');
		expect(browserChipAccessibleName({ host: null })).toBe('Reopen the shared browser');
	});

	it('reads the server field and drops anything that is not one', () => {
		expect(parseBrowserChip({ host: 'github.com' })).toEqual({ host: 'github.com' });
		expect(parseBrowserChip({ host: null })).toEqual({ host: null });
		expect(parseBrowserChip({ host: '' })).toEqual({ host: null });
		for (const raw of [undefined, null, 'github.com', 3, { label: 'x' }, { host: 3 }]) {
			expect(parseBrowserChip(raw)).toBeNull();
		}
	});
});
