import { describe, expect, it } from 'vitest';
import { siteLabels } from './siteLabel';

const SCHEME = 'https:' + '//';

describe('siteLabels', () => {
	const long = `${SCHEME}accounts.northwind-regional-supply-cooperative.example.com`;
	const shown = 'accounts.northwind-regional-supply-cooperative.example.com';

	it('starts with the whole site without its scheme', () => {
		expect(siteLabels(long, 'example.com')[0]).toBe(shown);
		expect(siteLabels(`${SCHEME}example.com`, 'example.com')).toEqual(['example.com']);
	});

	it('cuts whole labels from the front, keeps the end, and always keeps the registrable domain whole', () => {
		const labels = siteLabels(long, 'example.com');
		expect(labels).toEqual([
			shown,
			'…northwind-regional-supply-cooperative.example.com',
			'…example.com',
		]);
		for (const label of labels) expect(label).not.toContain(SCHEME);
		for (const label of labels.slice(1)) {
			expect(label.startsWith('…')).toBe(true);
			expect(label.match(/…/g)).toHaveLength(1);
			expect(label).not.toMatch(/… /);
			// What follows the cut is whole labels ending in the whole domain.
			const tail = label.slice(1);
			expect(tail === 'example.com' || shown.endsWith(`.${tail}`)).toBe(true);
		}
	});

	it('keeps a port with the domain and a public-suffix domain whole', () => {
		const labels = siteLabels(`${SCHEME}login.shop.example.co.uk:8443`, 'example.co.uk');
		expect(labels).toEqual(['login.shop.example.co.uk:8443', '…shop.example.co.uk:8443', '…example.co.uk:8443']);
	});

	it('keeps the whole host without a registrable domain or when the domain is the host itself', () => {
		expect(siteLabels(`${SCHEME}192.168.1.20`, null)).toEqual(['192.168.1.20']);
		expect(siteLabels(`${SCHEME}192.168.1.20`, '192.168.1.20')).toEqual(['192.168.1.20']);
	});
});
