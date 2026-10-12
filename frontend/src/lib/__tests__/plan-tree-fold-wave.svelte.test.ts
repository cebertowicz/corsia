import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import * as foldWave from '$lib/components/plans/foldWave';
import { STAGGER_MS, newlyMatching } from '$lib/components/plans/foldWave';
import { heldIds, type PaneView } from '$lib/components/plans/foldMotion.svelte';
import type { FoldedWork } from '$lib/components/plans/planFoldLevels';
import { node } from './fixtures/planTreeNode';
import { withFolds, withLiveFolds } from './fixtures/planFolds';

/**
 * When rows fold, every one of them animates -- a check beat, a slide to its
 * level's count, a fold into it -- one at a time, top to bottom, a fixed
 * `STAGGER_MS` apart. Never some animated and the rest snapped. Reduced
 * motion shows the settled state only, and only a fold the user can watch
 * happen plays: the first render of old history does not.
 */

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let animate: ReturnType<typeof vi.fn>;
let reducedMotion = false;
const originalAnimate = Element.prototype.animate;
const originalMatchMedia = window.matchMedia;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	reducedMotion = false;
	animate = vi.fn(function () {
		return { cancel: vi.fn(), finished: Promise.resolve() } as unknown as Animation;
	});
	Element.prototype.animate = animate as unknown as typeof Element.prototype.animate;
	window.matchMedia = ((query: string) => ({
		matches: reducedMotion && query.includes('prefers-reduced-motion'),
		media: query, addEventListener() {}, removeEventListener() {},
	})) as unknown as typeof window.matchMedia;
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	Element.prototype.animate = originalAnimate;
	window.matchMedia = originalMatchMedia;
});

function finished(count: number, folded: boolean): OperatorNode[] {
	return [
		node({ id: 'open', title: 'Open task' }),
		...Array.from({ length: count }, (_, i) => node({
			id: `done-${i}`, title: `Done ${i}`, state: 'accepted', folded,
			last_transition_at: new Date(Date.UTC(2026, 8, 17) - i * 60_000).toISOString(),
		})),
	];
}

/** The animations run on fold ghosts: one per row that folded, with its start delay. */
function ghostDelays(): number[] {
	return animate.mock.contexts
		.map((element, index) => ({ element: element as Element, options: animate.mock.calls[index][1] }))
		.filter(({ element }) => element.classList.contains('fold-ghost'))
		.map(({ options }) => (options as KeyframeAnimationOptions).delay as number);
}

async function foldAll(count: number) {
	const props = $state({ nodes: finished(count, false) });
	component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
	await tick();
	flushSync();
	props.nodes = finished(count, true);
	await tick();
	flushSync();
}

describe('PlanTree — the fold wave', () => {
	it('animates every row that folds, each a fixed stagger after the last', async () => {
		await foldAll(3);
		expect(ghostDelays()).toEqual([0, STAGGER_MS, 2 * STAGGER_MS]);
		expect(host.querySelector('.fold-row')?.textContent).toContain('3 completed');
	});

	it('starts the rows in on-screen order, top to bottom, not plan order', async () => {
		// Finished rows render newest settled first, so plan order is the reverse of screen order.
		const at = (minute: number) => new Date(Date.UTC(2026, 8, 17, 12, minute)).toISOString();
		const rows = (folded: boolean) => [
			node({ id: 'older', title: 'Older', state: 'accepted', folded, last_transition_at: at(1) }),
			node({ id: 'newer', title: 'Newer', state: 'accepted', folded, last_transition_at: at(2) }),
			node({ id: 'open', title: 'Open task' }),
		];
		const props = $state({ nodes: rows(false) });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		props.nodes = rows(true);
		await tick();
		flushSync();
		const order = animate.mock.contexts
			.map((element, index) => ({ element: element as Element, options: animate.mock.calls[index][1] }))
			.filter(({ element }) => element.classList.contains('fold-ghost'))
			.sort((a, b) => (a.options as KeyframeAnimationOptions).delay as number - ((b.options as KeyframeAnimationOptions).delay as number))
			.map(({ element }) => element.querySelector('.row-title')?.textContent);
		expect(order).toEqual(['Newer', 'Older']);
	});

	it('keeps playing when a refresh lands mid-wave (found on the live pane)', async () => {
		const props = $state({ nodes: finished(3, false) });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		props.nodes = finished(3, true);
		await tick();
		flushSync();
		const played = animate.mock.results.map((result) => result.value as { cancel: ReturnType<typeof vi.fn> });
		props.nodes = finished(3, true);
		await tick();
		flushSync();
		expect(played.length).toBeGreaterThan(0);
		expect(played.every((animation) => animation.cancel.mock.calls.length === 0)).toBe(true);
	});

	it('removes every ghost once its fold lands', async () => {
		await foldAll(3);
		await Promise.resolve();
		await tick();
		expect(host.querySelectorAll('.fold-ghost')).toHaveLength(0);
	});

	const played = () => animate.mock.results.map((result) => result.value as { cancel: ReturnType<typeof vi.fn> });
	const unmountTree = () => { unmount(component!); component = null; };

	it('lets go of a wave once it has ended, so unmount leaves it alone', async () => {
		await foldAll(2);
		await new Promise((resolve) => setTimeout(resolve, 0));
		unmountTree();
		expect(played().length).toBeGreaterThan(0);
		expect(played().every((animation) => animation.cancel.mock.calls.length === 0)).toBe(true);
	});

	it('stops a wave still playing when the tree unmounts', async () => {
		animate.mockImplementation(() => ({ cancel: vi.fn(), finished: new Promise(() => {}) }) as unknown as Animation);
		await foldAll(2);
		await new Promise((resolve) => setTimeout(resolve, 0));
		unmountTree();
		// The ghost's check beat goes with its ghost; every wave animation is cancelled.
		const ghosts = played().filter((_, index) => (animate.mock.contexts[index] as Element).classList.contains('fold-ghost'));
		expect(ghosts).toHaveLength(2);
		expect(ghosts.every((animation) => animation.cancel.mock.calls.length === 1)).toBe(true);
	});

	it('does not replay old history on the first render', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: finished(8, true) }) });
		await tick();
		flushSync();
		expect(ghostDelays()).toHaveLength(0);
		expect(host.querySelector('.fold-row')?.textContent).toContain('8 completed');
	});

	it('honours reduced motion: no motion, just the settled state', async () => {
		reducedMotion = true;
		await foldAll(4);
		expect(animate).not.toHaveBeenCalled();
		expect(host.querySelector('.fold-row')?.textContent).toContain('4 completed');
		expect(host.querySelectorAll('.fold-ghost')).toHaveLength(0);
	});

	it('plays the same wave -- glide only, no ghost -- when a row only sinks into the finished group', async () => {
		const captureSpy = vi.spyOn(foldWave, 'captureFold');
		const props = $state({
			nodes: [
				node({ id: 'a', title: 'Task A' }),
				node({ id: 'b', title: 'Task B', state: 'active' }),
			],
		});
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		// 'b' becomes terminal -- it sinks to the bottom of its level as its own
		// row, the same slide a fold uses, but it is not folded into a count, so
		// it gets no ghost.
		props.nodes = [
			node({ id: 'a', title: 'Task A' }),
			node({ id: 'b', title: 'Task B', state: 'accepted' }),
		];
		await tick();
		flushSync();
		expect(captureSpy).toHaveBeenCalled();
		expect(ghostDelays()).toHaveLength(0);
		expect(host.querySelector('.fold-row')).toBeNull();
	});
});

describe('newlyMatching', () => {
	it('returns rows whose predicate flipped from false to true since the last render', () => {
		const previous = new Map([['a', false], ['b', true]]);
		const nodes = [node({ id: 'a', title: 'A' }), node({ id: 'b', title: 'B' })];
		expect(newlyMatching(previous, nodes, () => true).map((n) => n.id)).toEqual(['a']);
	});

	it('replays nothing on the first render (no previous map)', () => {
		expect(newlyMatching(null, [node({ id: 'a', title: 'A' })], () => true)).toEqual([]);
	});
});


/**
 * The pane holds open every folded row its read lists that it has not yet shown
 * fold, and folds them when the pane releases them (`PaneView.released`). A
 * level's fold row counts what the server counted, less the rows held open.
 * Without a pane view (an Activity card, a drill-down) the tree shows the
 * server's folds.
 */
describe('PlanTree — only what is new since the last look', () => {
	const T0 = Date.UTC(2026, 8, 17);
	const iso = (ms: number) => new Date(ms).toISOString();

	const rowIds = () => [...host.querySelectorAll<HTMLElement>('.tree-row[data-flip-key]:not(.fold-row)')]
		.map((row) => row.dataset.flipKey);
	const foldText = () => host.querySelector('.fold-row')?.textContent ?? '';
	const seen = (...ids: string[]): PaneView => ({ released: new Set(ids) });

	async function open(nodes: OperatorNode[], paneView: PaneView | null, folded?: FoldedWork) {
		const props = $state({ nodes, paneView, folded });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		return props;
	}

	async function release(props: { paneView: PaneView | null }, ...ids: string[]) {
		props.paneView = seen(...ids);
		await tick();
		flushSync();
	}

	it('holds every listed folded row open, and counts only what the server has beyond them', async () => {
		const server = { planId: 'plan-1', levels: [{ parent_id: null, done: 15, failed: 0, closed: 0, more: 3 }] };
		await open(finished(12, true), seen(), server);
		expect(rowIds()).toEqual(['open', ...Array.from({ length: 12 }, (_, i) => `done-${i}`)]);
		expect(foldText()).toContain('3 completed');
		expect(ghostDelays()).toHaveLength(0);
	});

	it('holds only rows not yet released, and folds them in one wave when the pane releases them', async () => {
		const props = await open(finished(4, true), seen('done-2', 'done-3'));
		expect(rowIds()).toEqual(['open', 'done-0', 'done-1']);
		expect(foldText()).toContain('2 completed');
		await release(props, 'done-0', 'done-1', 'done-2', 'done-3');
		expect(ghostDelays()).toEqual([0, STAGGER_MS]);
		expect(rowIds()).toEqual(['open']);
		expect(foldText()).toContain('4 completed');
	});

	it('replays nothing when the view has shown every folded row', async () => {
		await open(finished(4, true), seen('done-0', 'done-1', 'done-2', 'done-3'));
		expect(rowIds()).toEqual(['open']);
		expect(foldText()).toContain('4 completed');
		expect(ghostDelays()).toHaveLength(0);
	});

	it('holds a row that finishes while the pane is open until the view releases it', async () => {
		const props = await open(
			[node({ id: 'open', title: 'Open task' }), node({ id: 'late', title: 'Late' })],
			seen(),
		);
		props.nodes = [
			node({ id: 'open', title: 'Open task' }),
			node({ id: 'late', title: 'Late', state: 'accepted', folded: true, last_transition_at: iso(T0 + 60_000) }),
		];
		await tick();
		flushSync();
		expect(rowIds()).toEqual(['open', 'late']);
		expect(ghostDelays()).toHaveLength(0);
		await release(props, 'late');
		expect(ghostDelays()).toEqual([0]);
		expect(rowIds()).toEqual(['open']);
	});

	it('shows the server folds without a pane view', async () => {
		await open(finished(4, true), null);
		expect(rowIds()).toEqual(['open']);
		expect(foldText()).toContain('4 completed');
	});

	it('honours reduced motion: the same rows fold, with no animation', async () => {
		reducedMotion = true;
		const props = await open(finished(4, true), seen());
		await release(props, 'done-0', 'done-1', 'done-2', 'done-3');
		expect(animate).not.toHaveBeenCalled();
		expect(rowIds()).toEqual(['open']);
	});
});

describe('heldIds', () => {
	it('holds the listed folded rows a view has not shown, and nothing without a view', () => {
		const nodes = [
			node({ id: 'phase', state: 'pending', folded: true }),
			node({ id: 'new', parent_id: 'phase', depth: 1, state: 'accepted', folded: true }),
			node({ id: 'old', state: 'accepted', folded: true }),
			node({ id: 'open', state: 'active' }),
		];
		expect([...heldIds(nodes, { released: new Set(['old']) })].sort()).toEqual(['new', 'phase']);
		expect(heldIds(nodes, null).size).toBe(0);
	});
});
