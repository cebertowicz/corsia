import { describe, expect, it } from 'vitest';
import { siteLabels } from './siteLabel';

const SCHEME = 'https:' + '//';

describe('siteLabels', () => {
	const long = `${SCHEME}accounts.northwind-regional-supply-cooperative.example.com`;

	it('starts with the whole site', () => {
		expect(siteLabels(long, 'example.com')[0]).toBe(long);
		expect(siteLabels(`${SCHEME}example.com`, 'example.com')).toEqual([`${SCHEME}example.com`, '…example.com']);
	});

	it('cuts only between whole segments and always keeps the registrable domain whole', () => {
		const labels = siteLabels(long, 'example.com');
		expect(labels).toEqual([
			long,
			'…accounts.northwind-regional-supply-cooperative.example.com',
			`${SCHEME}…northwind-regional-supply-cooperative.example.com`,
			'…northwind-regional-supply-cooperative.example.com',
			`${SCHEME}accounts…example.com`,
			`${SCHEME}…example.com`,
			'…example.com',
		]);
		for (const label of labels.slice(1)) {
			expect(label).toMatch(/example\.com$/);
			expect(label.match(/…/g)).toHaveLength(1);
			expect(label).not.toMatch(/ …|… /);
			const [head, tail] = label.split('…');
			// The head is whole segments: nothing, the scheme, or the scheme and whole labels.
			expect(head === '' || head === SCHEME || long.startsWith(`${head}.`)).toBe(true);
			// The tail is whole labels ending in the whole domain.
			expect(tail === 'example.com' || long.endsWith(`.${tail}`) || long.endsWith(`//${tail}`)).toBe(true);
		}
	});

	it('keeps a port with the domain and a public-suffix domain whole', () => {
		const labels = siteLabels(`${SCHEME}login.shop.example.co.uk:8443`, 'example.co.uk');
		expect(labels.at(-1)).toBe('…example.co.uk:8443');
		expect(labels).toContain(`${SCHEME}…example.co.uk:8443`);
		expect(labels.every((label) => label.endsWith('example.co.uk:8443'))).toBe(true);
	});

	it('keeps the whole host as the tail without a domain', () => {
		expect(siteLabels(`${SCHEME}192.168.1.20`, null)).toEqual([`${SCHEME}192.168.1.20`, '…192.168.1.20']);
	});
});
