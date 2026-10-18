import { afterEach, describe, expect, it } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import Field from './Field.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const control = createRawSnippet(() => ({ render: () => '<textarea id="ctl"></textarea>' }));

function render(props: Record<string, unknown> = {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(Field, { target: host, props: { label: 'Comment', labelFor: 'ctl', children: control, ...props } });
	flushSync();
	return host.querySelector<HTMLElement>('.field')!;
}
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });

describe('Field variants', () => {
	it('adds no variant class by default', () => {
		const field = render();
		expect(field.classList.contains('chip')).toBe(false);
		expect(field.classList.contains('nested')).toBe(false);
		expect(field.classList.contains('hide-label')).toBe(false);
	});

	it('marks the chip and nested variants', () => {
		expect(render({ variant: 'chip' }).classList.contains('chip')).toBe(true);
		unmount(component!); host.remove();
		const nested = render({ variant: 'nested' });
		expect(nested.classList.contains('nested')).toBe(true);
		expect(nested.classList.contains('chip')).toBe(false);
	});

	it('keeps a hidden label as the control accessible name', () => {
		const field = render({ variant: 'nested', hideLabel: true });
		expect(field.classList.contains('hide-label')).toBe(true);
		const label = field.querySelector<HTMLLabelElement>('label.l')!;
		expect(label.textContent).toBe('Comment');
		expect(label.htmlFor).toBe('ctl');
	});

	it('carries the error state for the shared error border', () => {
		const field = render({ variant: 'nested', error: 'Too long' });
		expect(field.classList.contains('has-error')).toBe(true);
		expect(field.querySelector('.err')!.textContent).toBe('Too long');
	});
});
