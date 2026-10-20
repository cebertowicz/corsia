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
		expect(label?.getAttribute('aria-label')).toBe('1,219 done');
		expect(label?.textContent).toBe('1.2k done');
		expect(label?.firstElementChild?.getAttribute('aria-hidden')).toBe('true');
	});
});
