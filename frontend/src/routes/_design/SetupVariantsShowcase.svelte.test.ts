import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import SetupVariantsShowcase from './SetupVariantsShowcase.svelte';

let host: HTMLDivElement;
let component: ReturnType<typeof mount>;

function click(label: string) {
	const button = Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.trim() === label);
	expect(button).toBeTruthy();
	button?.click();
	flushSync();
}

function longRail() {
	const row = Array.from(host.querySelectorAll<HTMLElement>('.evidence-grid .rail .row')).find((item) => item.textContent?.includes('A very long setup step name'));
	expect(row).toBeTruthy();
	return row!;
}

afterEach(async () => {
	if (component) await unmount(component);
	host.remove();
});

describe('SetupVariantsShowcase evidence fixture', () => {
	it('renders long RailRow and ListRow content and cycles its recordable states', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(SetupVariantsShowcase, { target: host });
		flushSync();

		expect(host.textContent).toContain('A very long setup step name that must truncate instead of pushing the rail wider');
		expect(host.textContent).toContain('A very long provider account name that must wrap or truncate without overlapping its ready action');
		expect(host.querySelector('.long-list .row-main.truncate')).toBeTruthy();
		expect(longRail().classList).toContain('todo');

		click('Advance step');
		expect(longRail().classList).toContain('current');
		click('Advance step');
		expect(longRail().classList).toContain('done');

		const progress = () => host.querySelector<HTMLElement>('.evidence-grid [role="progressbar"]');
		expect(progress()?.getAttribute('aria-valuenow')).toBe('34');
		click('Advance background job');
		expect(progress()?.getAttribute('aria-valuenow')).toBe('68');
		click('Advance background job');
		expect(host.textContent).toContain('A very long background model download name');
		expect(progress()).toBeNull();
		click('Advance background job');
		expect(host.textContent).toContain('Couldn’t finish');
	});
});
