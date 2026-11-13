import { afterEach, describe, expect, it } from 'vitest';
import { mount, unmount } from 'svelte';
import MiddleName from './MiddleName.svelte';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

function render(name: string) {
	host = document.body.appendChild(document.createElement('div'));
	component = mount(MiddleName, { target: host, props: { name } });
	return host.querySelector<HTMLElement>('.name')!;
}

afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });

describe('MiddleName', () => {
	it('cuts a long name in the middle: the head keeps a floor of its first letter and the ellipsis, and the tail stays whole', () => {
		const name = render('Responsive Design Review and Accessibility Coordination Agent');
		const head = name.querySelector('.name-head')!;
		expect(name.classList.contains('cut')).toBe(true);
		expect(head.textContent).toBe('Responsive Design Review and Accessibility Coordination A');
		expect(name.querySelector('.name-tail')?.textContent).toBe('gent');
	});

	it('leaves a short name whole, with no floor that would pad it', () => {
		const name = render('Jo');
		expect(name.classList.contains('cut')).toBe(false);
		expect(name.querySelector('.name-tail')).toBeNull();
	});
});
