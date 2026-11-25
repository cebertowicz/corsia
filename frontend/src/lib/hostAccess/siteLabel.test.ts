import { describe, expect, it } from 'vitest';
import { rowLayout, siteLabels } from './siteLabel';

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

describe('rowLayout', () => {
	// A 393 px phone: 343 px of row, 12 px gaps, a 13 px name, "Review →" 59 px, the badge 122 px, "…example.com" 91 px.
	const phone = { room: 343, gap: 12, em: 13, siteMin: 91 };

	it('keeps the name at 8em on one row when the site fits beside it at its domain', () => {
		expect(rowLayout({ ...phone, fixed: 59, items: 3 })).toEqual({ stacked: false, ownerFloor: 104 });
		expect(rowLayout({ room: 1100, gap: 12, em: 13, siteMin: 91, fixed: 160, items: 4 })).toEqual({ stacked: false, ownerFloor: 104 });
	});

	it('lowers the name\'s floor below 8em, not under 4em, before the site leaves the row', () => {
		expect(rowLayout({ ...phone, fixed: 59, items: 3, siteMin: 200 })).toEqual({ stacked: false, ownerFloor: 60 });
		expect(rowLayout({ ...phone, fixed: 59, items: 3, siteMin: 208 })).toEqual({ stacked: false, ownerFloor: 52 });
	});

	it('puts the site on its own line when the name would get less than 4em, and the name keeps the row', () => {
		// Out of date: the badge joins "Review →" on the row.
		expect(rowLayout({ ...phone, fixed: 59 + 122, items: 4 })).toEqual({ stacked: true, ownerFloor: 104 });
		// A long registrable domain that only fits on a line of its own.
		expect(rowLayout({ ...phone, fixed: 59, items: 3, siteMin: 340 })).toEqual({ stacked: true, ownerFloor: 104 });
		expect(rowLayout({ room: 200, gap: 12, em: 13, siteMin: 91, fixed: 59 + 122, items: 4 })).toEqual({ stacked: true, ownerFloor: 0 });
	});
});
