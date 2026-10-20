import { mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import PlanFoldMark from '$lib/components/plans/PlanFoldMark.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

describe('PlanFoldMark', () => {
	it('exposes the exact done count once while compacting the visible count', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(PlanFoldMark, { target: host, props: { counts: { done: 1_219, failed: 0, closed: 0 } } });

		const label = host.querySelector('.fold-label');
		const [shown, spoken] = [...(label?.children ?? [])];
		expect(shown.textContent).toBe('1.2k done');
		expect(shown.getAttribute('aria-hidden')).toBe('true');
		expect(spoken.textContent).toBe('1,219 done');
		expect(spoken.classList.contains('sr-only')).toBe(true);
	});
});
