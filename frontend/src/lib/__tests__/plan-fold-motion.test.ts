import { flushSync, mount, tick, unmount } from 'svelte';
import { writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const reduced = vi.hoisted(() => ({ store: null as null | { set: (value: boolean) => void } }));
vi.mock('$lib/stores/viewport', async (importOriginal) => {
	const { writable: make } = await import('svelte/store');
	const original = await importOriginal<Record<string, unknown>>();
	const store = make(false);
	reduced.store = store;
	return { ...original, prefersReducedMotion: store };
});

import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { blockSlot } from '$lib/slotMotion';
import slotSource from '$lib/slotMotion.ts?raw';
import treeSource from '$lib/components/plans/PlanTree.svelte?raw';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

/** #428: fold rows pop in and out. They slide on the motion tokens at the person's request, and stay instant under reduced motion. */

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const animate = vi.fn();

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	document.documentElement.style.setProperty('--dur-tight', '120ms');
	document.documentElement.style.setProperty('--dur-standard', '220ms');
	document.documentElement.style.setProperty('--ease-snap', 'snap');
	document.documentElement.style.setProperty('--ease-entrance', 'entrance');
	animate.mockReset();
	animate.mockImplementation(() => ({ cancel() {}, finish() {}, finished: Promise.resolve() }));
	Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, writable: true, value: animate });
	reduced.store?.set(false);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	delete (HTMLElement.prototype as unknown as { animate?: unknown }).animate;
	document.documentElement.removeAttribute('style');
});

const run = (direction: 'in' | 'out', params?: { off?: boolean }) => {
	const el = document.createElement('div');
	document.body.appendChild(el);
	const result = blockSlot(el, params, { direction });
	el.remove();
	return result;
};

describe('blockSlot', () => {
	it('arrives growing from nothing on the standard duration and entrance easing', () => {
		expect(run('in')).toMatchObject({ duration: 220 });
		const [keyframes, options] = animate.mock.calls[0];
		expect(keyframes[0]).toMatchObject({ height: '0px', opacity: 0, paddingTop: '0px', marginBottom: '0px' });
		expect(keyframes[1]).toMatchObject({ opacity: 1 });
		for (const keyframe of keyframes) expect(keyframe).not.toHaveProperty('easing');
		expect(options).toMatchObject({ duration: 220, easing: 'entrance' });
	});

	it('closes over 70% of the standard duration with fade and grid rows overlapping', () => {
		expect(run('out')).toMatchObject({ duration: 154 });
		const [keyframes, options] = animate.mock.calls[0];
		expect(keyframes.at(-1)).toMatchObject({ height: '0px', gridTemplateRows: '0fr', opacity: 0, minHeight: '0px' });
		expect(options).toMatchObject({ duration: 154, easing: 'snap', fill: 'forwards' });
	});

	it('puts easing on the block exit effect, never an individual keyframe', () => {
		run('out');
		const [exitFrames, exitOptions] = animate.mock.calls[0];
		expect(exitOptions).toMatchObject({ easing: 'snap' });
		for (const keyframe of exitFrames) expect(keyframe).not.toHaveProperty('easing');
	});

	it('crossfades over the tight duration under reduced motion without resizing', () => {
		reduced.store?.set(true);
		expect(run('in')).toMatchObject({ duration: 120 });
		expect(run('out')).toMatchObject({ duration: 120 });
		for (const [keyframes, options] of animate.mock.calls) {
			expect(keyframes).toEqual(expect.arrayContaining([expect.objectContaining({ opacity: expect.any(Number) })]));
			expect(options).toMatchObject({ duration: 120, easing: 'snap' });
			expect(keyframes).not.toEqual(expect.arrayContaining([expect.objectContaining({ height: expect.any(String) })]));
		}
	});

	it('plays nothing when told it is off, or where animations are unsupported', () => {
		expect(run('in', { off: true })).toMatchObject({ duration: 0 });
		expect(run('out', { off: true })).toMatchObject({ duration: 0 });
		Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, writable: true, value: undefined });
		expect(run('in')).toMatchObject({ duration: 0 });
		expect(animate).not.toHaveBeenCalled();
	});

	it('reads only motion tokens, never a raw duration', () => {
		expect(slotSource).not.toMatch(/\b\d+(\.\d+)?ms\b/);
		expect(slotSource).toMatch(/--dur-standard/);
		expect(slotSource).toMatch(/--ease-entrance/);
	});
});

function hoursAgo(hours: number): string {
	return new Date(Date.UTC(2026, 8, 18, 12) - hours * 3_600_000).toISOString();
}

function folded(count: number): OperatorNode[] {
	return Array.from({ length: count }, (_, i) => node({
		id: `old-${i}`, title: `Old ${i}`, state: 'accepted', folded: true, last_transition_at: hoursAgo(30 + i),
	}));
}

async function press(label: RegExp): Promise<void> {
	const button = [...host.querySelectorAll('button')].find((item) => label.test(item.textContent?.trim() ?? ''));
	expect(button, `button ${label}`).toBeTruthy();
	button!.click();
	await tick();
	flushSync();
}

describe('PlanTree fold rows', () => {
	function mountFolded() {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: [node({ id: 'open', title: 'Open task' }), ...folded(8)] }) });
		flushSync();
	}

	it('slides each row in when the person shows a page of finished steps', async () => {
		mountFolded();
		expect(animate).not.toHaveBeenCalled();
		await press(/^Show 5$/);
		const rows = host.querySelectorAll('.tree-row:not(.fold-row)').length - 1;
		expect(rows).toBe(5);
		const grown = animate.mock.calls.filter(([frames]) => frames[0]?.height === '0px');
		expect(grown).toHaveLength(5);
	});

	it('slides the rows out when the person collapses them', async () => {
		mountFolded();
		await press(/^Show 5$/);
		animate.mockClear();
		await press(/^Collapse$/);
		const closed = animate.mock.calls.filter(([frames]) => frames.at(-1)?.height === '0px');
		expect(closed).toHaveLength(5);
	});

	it('does not animate when a row is revealed for focus, or on first render', () => {
		mountFolded();
		(component as unknown as { revealNode: (id: string) => boolean }).revealNode('old-0');
		flushSync();
		expect(animate).not.toHaveBeenCalled();
	});

	it('crossfades fold rows under reduced motion while the drawer height snaps', async () => {
		reduced.store?.set(true);
		mountFolded();
		await press(/^Show 5$/);
		await press(/^Collapse$/);
		const fades = animate.mock.calls.filter(([frames]) => frames[0]?.opacity !== undefined && frames.every((frame: Keyframe) => frame.height === undefined));
		expect(fades).toHaveLength(10);
	});

	it('hands the person-initiated changes to blockSlot and leaves the fold wave alone', () => {
		expect(treeSource).toMatch(/in:blockSlot\|global=\{\{ off: !userPaging \}\}/);
		expect(treeSource).toMatch(/out:blockSlot\|global=\{\{ off: !userPaging \}\}/);
	});
});

describe('PlanTree disclosure motion', () => {
	function mountDisclosures() {
		component = mount(PlanTree, {
			target: host,
			props: withFolds({ nodes: [
				node({ id: 'detail', title: 'Detail task' }),
				node({ id: 'branch', title: 'Branch task', branch_progress: { done: 0, total: 1 } }),
				node({ id: 'child', title: 'Branch child', parent_id: 'branch', depth: 1 }),
			] }),
		});
		flushSync();
	}

	async function pressRow(title: string): Promise<void> {
		const button = [...host.querySelectorAll<HTMLButtonElement>('.row-body')].find((item) => item.textContent?.includes(title));
		expect(button, `row body ${title}`).toBeTruthy();
		button!.click();
		await tick();
		flushSync();
	}

	it('uses the shared press-gated block motion for details and branch children', async () => {
		mountDisclosures();
		await pressRow('Detail task');
		expect(host.querySelector('.node-details')).toBeTruthy();
		expect(animate.mock.calls.filter(([frames]) => frames[0]?.height === '0px')).toHaveLength(1);
		animate.mockClear();
		await pressRow('Detail task');
		expect([...host.querySelectorAll<HTMLButtonElement>('.row-body')].find((item) => item.textContent?.includes('Detail task'))?.getAttribute('aria-expanded')).toBe('false');
		expect(animate.mock.calls.filter(([frames]) => frames.at(-1)?.height === '0px')).toHaveLength(1);

		animate.mockClear();
		const branch = host.querySelector<HTMLButtonElement>('.branch-toggle');
		expect(branch).toBeTruthy();
		branch!.click();
		await tick();
		flushSync();
		expect(host.textContent).toContain('Branch child');
		expect(animate.mock.calls.filter(([frames]) => frames[0]?.height === '0px')).toHaveLength(1);
		animate.mockClear();
		branch!.click();
		await tick();
		flushSync();
		expect(branch!.getAttribute('aria-expanded')).toBe('false');
		expect(animate.mock.calls.filter(([frames]) => frames.at(-1)?.height === '0px')).toHaveLength(1);
	});

	it('does not leave a detail slot after rapid repeated presses', async () => {
		mountDisclosures();
		const button = [...host.querySelectorAll<HTMLButtonElement>('.row-body')].find((item) => item.textContent?.includes('Detail task'));
		expect(button).toBeTruthy();
		button!.click();
		button!.click();
		await tick();
		flushSync();
		expect(host.querySelectorAll('.details-slot')).toHaveLength(0);
	});

	it('keeps press disclosures instant only under reduced motion', async () => {
		reduced.store?.set(true);
		mountDisclosures();
		await pressRow('Detail task');
		const fades = animate.mock.calls.filter(([frames]) => frames[0]?.opacity !== undefined && frames.every((frame: Keyframe) => frame.height === undefined));
		expect(fades).toHaveLength(1);
	});
});
