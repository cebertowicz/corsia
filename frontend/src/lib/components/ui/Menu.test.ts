import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import Menu, { type MenuItem } from './Menu.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const ITEMS: MenuItem[] = [
	{ id: 'pin', label: 'Pin' },
	{ id: 'rename', label: 'Rename', disabled: true },
	{ id: 'remove', label: 'Remove', danger: true },
];
function render(props: { items?: MenuItem[]; x?: number; y?: number; busy?: boolean; note?: string } = {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	const onSelect = vi.fn(); const onClose = vi.fn();
	component = mount(Menu, { target: host, props: { ariaLabel: 'Actions', items: props.items ?? ITEMS, x: props.x ?? 20, y: props.y ?? 20, busy: props.busy, note: props.note, onSelect, onClose } });
	flushSync(); return { onSelect, onClose };
}
const menu = () => document.body.querySelector<HTMLElement>('[role="menu"]')!;
const entries = () => [...menu().querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
const key = (k: string) => menu().dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); vi.restoreAllMocks(); });

describe('Menu', () => {
	it('portals to the body and focuses the first item', () => {
		render();
		expect(menu().parentElement).toBe(document.body);
		expect(document.activeElement).toBe(entries()[0]);
	});

	it('moves focus with the arrows and wraps at either end', () => {
		render({ items: [ITEMS[0], ITEMS[2]] });
		key('ArrowDown'); expect(document.activeElement).toBe(entries()[1]);
		key('ArrowDown'); expect(document.activeElement).toBe(entries()[0]);
		key('ArrowUp'); expect(document.activeElement).toBe(entries()[1]);
	});

	it('reports the chosen verb, draws a destructive one in red and disables what is unavailable', () => {
		const { onSelect } = render();
		entries()[0].click();
		expect(onSelect).toHaveBeenCalledWith('pin');
		expect(entries()[1].disabled).toBe(true);
		expect(entries()[2].classList.contains('danger')).toBe(true);
	});

	it('holds every item while an action is in flight', () => {
		render({ busy: true });
		expect(entries().every((item) => item.disabled)).toBe(true);
	});

	it('closes on Escape and on a click outside', () => {
		const { onClose } = render();
		key('Escape');
		expect(onClose).toHaveBeenCalledOnce();
		document.body.querySelector<HTMLElement>('.menu-backdrop')!.click();
		expect(onClose).toHaveBeenCalledTimes(2);
	});

	it('stays inside the window when opened near its edge', () => {
		render({ x: window.innerWidth - 4, y: window.innerHeight - 4 });
		expect(Number.parseFloat(menu().style.left)).toBeLessThanOrEqual(window.innerWidth - 220 - 8);
		expect(Number.parseFloat(menu().style.top)).toBeLessThan(window.innerHeight - 4);
	});

	it('keeps a note line inside the window too, by its rendered height', () => {
		vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
			return this.getAttribute('role') === 'menu' ? 200 : 0;
		});
		render({ y: window.innerHeight - 4, note: 'Moves with its plan' });
		expect(Number.parseFloat(menu().style.top)).toBe(window.innerHeight - 208);
	});
});
