import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import * as foldWave from '$lib/components/plans/foldWave';
import { FOLD_BEAT_MS, STAGGER_MS, newlyLater, newlyMatching } from '$lib/components/plans/foldWave';
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
const originalRect = HTMLElement.prototype.getBoundingClientRect;

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
	HTMLElement.prototype.getBoundingClientRect = originalRect;
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
		expect(host.querySelector('.fold-row')?.textContent).toContain('3 done');
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
		expect(host.querySelector('.fold-row')?.textContent).toContain('8 done');
	});

	it('honours reduced motion: no motion, just the settled state', async () => {
		reducedMotion = true;
		await foldAll(4);
		expect(animate).not.toHaveBeenCalled();
		expect(host.querySelector('.fold-row')?.textContent).toContain('4 done');
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

/**
 * An agent moves one task below the cut and back. It is neither folded nor
 * removed -- it stays on screen at a new place -- so it rides the same wave as a
 * sinking row: captured before the DOM moves, glided after it, and never
 * ghosted into a count. (`foldMotion`, `foldWave`.)
 */
describe('PlanTree — a task crossing the Later cut', () => {
	const ROW = 38;

	/** jsdom lays nothing out: give every row a top from its order in the tree, so a real move is measurable. */
	function layOutRowsByOrder() {
		HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
			const index = [...host.querySelectorAll<HTMLElement>('[data-flip-key]')].indexOf(this);
			const top = index < 0 ? 0 : index * ROW;
			return { top, bottom: top + ROW, left: 0, right: 300, width: 300, height: ROW, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
		};
	}

	const rowTitles = () => [...host.querySelectorAll<HTMLElement>('.tree-row')].map((row) =>
		row.classList.contains('scheduled-row') ? `{${row.querySelector('.row-title')?.textContent}}` : row.querySelector('.row-title')?.textContent ?? '?');

	/** The glide played on one row's own element: its keyframes and options, or undefined. */
	const glideOf = (title: string) => animate.mock.contexts
		.map((el, index) => ({ el: el as HTMLElement, keyframes: animate.mock.calls[index][0] as Keyframe[], options: animate.mock.calls[index][1] as KeyframeAnimationOptions }))
		.filter(({ el, keyframes }) => el.querySelector(':scope > .row-body .row-title')?.textContent === title
			&& typeof keyframes[0]?.transform === 'string' && keyframes[0].transform.startsWith('translateY'))
		.at(-1);

	/** 'Task B' above the cut, then below it; 'Task C' is already Later, so the group exists and stays open. */
	const withLater = (bLater: boolean): OperatorNode[] => [
		node({ id: 'a', title: 'Task A' }),
		node({ id: 'b', title: 'Task B', later: bLater }),
		node({ id: 'c', title: 'Task C', later: true }),
	];

	async function openTreeWithGroupShown() {
		const props = $state({ nodes: withLater(false) });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		host.querySelector<HTMLButtonElement>('.scheduled-row button.details-toggle')!.click();
		await tick();
		flushSync();
		expect(rowTitles()).toEqual(['Task A', 'Task B', '{Later}', 'Task C']);
		return props;
	}

	it('glides it down into the group and back up out of it, with no ghost', async () => {
		layOutRowsByOrder();
		const captureSpy = vi.spyOn(foldWave, 'captureFold');
		const props = await openTreeWithGroupShown();

		captureSpy.mockClear();
		animate.mockClear();
		props.nodes = withLater(true);
		await tick();
		flushSync();
		expect(rowTitles()).toEqual(['Task A', '{Later}', 'Task B', 'Task C']);
		expect(captureSpy).toHaveBeenCalledTimes(1);
		expect(host.querySelectorAll('.fold-ghost')).toHaveLength(0);
		// It left row 2 for the first row under the group's own row: one row down,
		// so it starts one row above where it now sits and slides into place.
		const down = glideOf('Task B');
		expect(down?.keyframes).toEqual([{ transform: `translateY(-${ROW}px)` }, { transform: 'none' }]);
		expect(down?.options).toMatchObject({ duration: FOLD_BEAT_MS.slide, delay: FOLD_BEAT_MS.check, fill: 'backwards' });

		captureSpy.mockClear();
		animate.mockClear();
		props.nodes = withLater(false);
		await tick();
		flushSync();
		expect(rowTitles()).toEqual(['Task A', 'Task B', '{Later}', 'Task C']);
		expect(captureSpy).toHaveBeenCalledTimes(1);
		expect(host.querySelectorAll('.fold-ghost')).toHaveLength(0);
		expect(glideOf('Task B')?.keyframes).toEqual([{ transform: `translateY(${ROW}px)` }, { transform: 'none' }]);
	});

	it('collapses it into the collapsed group: a ghost into the group row, which is never re-created', async () => {
		layOutRowsByOrder();
		const props = $state({ nodes: withLater(false) });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		expect(rowTitles()).toEqual(['Task A', 'Task B', '{Later}']); // collapsed: its members have no rows of their own
		const header = host.querySelector('.scheduled-row')!;

		animate.mockClear();
		props.nodes = withLater(true);
		await tick();
		flushSync();

		expect(rowTitles()).toEqual(['Task A', '{Later}']);
		// The group row is the same element throughout: the count changes inside it.
		expect(host.querySelector('.scheduled-row')).toBe(header);
		const played = animate.mock.contexts
			.map((el, index) => ({ el: el as HTMLElement, keyframes: animate.mock.calls[index][0] as Keyframe[] }))
			.filter(({ el }) => el.classList.contains('fold-ghost'));
		// The leaving row is cloned and flown into the group's own row. A clone is only
		// ever added once a target is found, so this also proves the group row is that target.
		expect(played).toHaveLength(1);
		expect(played[0].el.textContent).toContain('Task B');
		expect(played[0].keyframes.at(-1)).toMatchObject({ transform: 'translateY(0px) scaleY(0.2)', opacity: 0 });
		// And it slides with the rows above rather than fading in, which is what left
		// an empty gap where it had been.
		const onHeader = animate.mock.contexts
			.map((el, index) => ({ el: el as HTMLElement, keyframes: animate.mock.calls[index][0] as Keyframe[] }))
			.filter(({ el }) => el === header).map(({ keyframes }) => keyframes);
		expect(onHeader).toEqual([[{ transform: `translateY(${ROW}px)` }, { transform: 'none' }]]);
	});

	it('aims the ghost at where the group row settles, not where its glide starts', async () => {
		layOutRowsByOrder();
		const rowRect = HTMLElement.prototype.getBoundingClientRect;
		// A gliding row measures where its FLIP transform draws it, as a browser does.
		HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
			const rect = rowRect.call(this);
			const index = animate.mock.contexts.lastIndexOf(this);
			const from = index < 0 ? undefined : (animate.mock.calls[index][0] as Keyframe[])[0]?.transform;
			const dy = typeof from === 'string' ? Number(from.match(/^translateY\((-?[\d.]+)px\)$/)?.[1] ?? 0) : 0;
			return { ...rect, top: rect.top + dy, bottom: rect.bottom + dy, y: rect.top + dy, toJSON: () => ({}) } as DOMRect;
		};
		const props = $state({ nodes: withLater(false) });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();

		animate.mockClear();
		props.nodes = withLater(true);
		await tick();
		flushSync();

		const ghost = animate.mock.contexts.findIndex((el) => (el as HTMLElement).classList.contains('fold-ghost'));
		// Task B left from the second slot (top ROW); the group row settles in that slot.
		expect((animate.mock.calls[ghost][0] as Keyframe[]).at(-1)).toMatchObject({ transform: 'translateY(0px) scaleY(0.2)' });
	});

	it('keeps the group row drawn over the arriving ghost, then lets it go', async () => {
		layOutRowsByOrder();
		let finish = () => {};
		const finished = new Promise<void>((resolve) => { finish = resolve; });
		animate.mockImplementation(() => ({ cancel: vi.fn(), finished }) as unknown as Animation);
		const props = $state({ nodes: withLater(false) });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		const header = host.querySelector<HTMLElement>('.scheduled-row')!;

		props.nodes = withLater(true);
		await tick();
		flushSync();

		// The ghost slides in under the group row rather than over it.
		expect(header.style.zIndex).toBe('2');
		expect(header.style.position).toBe('relative');
		expect(header.style.background).not.toBe('');
		finish();
		await finished;
		await tick();
		expect(header.getAttribute('style') ?? '').not.toMatch(/z-index|position|background/);
	});

	it('keeps the shrinking tree tall until the group row has slid up, so the surface never clips it', async () => {
		layOutRowsByOrder();
		const rowRect = HTMLElement.prototype.getBoundingClientRect;
		// The tree is as tall as its rows, as the surface around it measures it.
		HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
			if (!this.classList.contains('plan-tree')) return rowRect.call(this);
			const height = this.querySelectorAll('[data-flip-key]').length * ROW;
			return { top: 0, bottom: height, left: 0, right: 300, width: 300, height, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
		};
		const props = $state({ nodes: withLater(false) });
		component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
		await tick();
		flushSync();
		const tree = host.querySelector('.plan-tree')!;

		animate.mockClear();
		props.nodes = withLater(true);
		await tick();
		flushSync();

		const onTree = animate.mock.contexts
			.map((el, index) => ({ el, keyframes: animate.mock.calls[index][0] as Keyframe[], options: animate.mock.calls[index][1] as KeyframeAnimationOptions }))
			.filter(({ el }) => el === tree);
		expect(onTree.map(({ keyframes }) => keyframes)).toEqual([[{ height: `${3 * ROW}px` }, { height: `${2 * ROW}px` }]]);
		// It eases on the rows' own beat, so the group row's bottom and the tree's arrive together.
		expect(onTree[0].options).toMatchObject({
			duration: foldWave.FOLD_BEAT_MS.slide, delay: foldWave.FOLD_BEAT_MS.check, fill: 'backwards',
		});
	});

	it('honours reduced motion: the crossing settles in place with no animation', async () => {
		layOutRowsByOrder();
		reducedMotion = true;
		const props = await openTreeWithGroupShown();
		animate.mockClear();
		props.nodes = withLater(true);
		await tick();
		flushSync();
		expect(animate).not.toHaveBeenCalled();
		expect(rowTitles()).toEqual(['Task A', '{Later}', 'Task B', 'Task C']);
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

describe('newlyLater', () => {
	const rows = (bLater: boolean) => [node({ id: 'a', title: 'A' }), node({ id: 'b', title: 'B', later: bLater })];

	it('names rows that crossed into or out of Later since the last render', () => {
		expect(newlyLater(new Map([['a', false], ['b', false]]), rows(true)).map((n) => n.id)).toEqual(['b']);
		expect(newlyLater(new Map([['a', false], ['b', true]]), rows(false)).map((n) => n.id)).toEqual(['b']);
	});

	it('replays nothing on the first render, and never a row it has not seen', () => {
		expect(newlyLater(null, rows(true))).toEqual([]);
		expect(newlyLater(new Map([['a', false]]), rows(true))).toEqual([]);
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
		expect(foldText()).toContain('3 done');
		expect(ghostDelays()).toHaveLength(0);
	});

	it('holds only rows not yet released, and folds them in one wave when the pane releases them', async () => {
		const props = await open(finished(4, true), seen('done-2', 'done-3'));
		expect(rowIds()).toEqual(['open', 'done-0', 'done-1']);
		expect(foldText()).toContain('2 done');
		await release(props, 'done-0', 'done-1', 'done-2', 'done-3');
		expect(ghostDelays()).toEqual([0, STAGGER_MS]);
		expect(rowIds()).toEqual(['open']);
		expect(foldText()).toContain('4 done');
	});

	it('replays nothing when the view has shown every folded row', async () => {
		await open(finished(4, true), seen('done-0', 'done-1', 'done-2', 'done-3'));
		expect(rowIds()).toEqual(['open']);
		expect(foldText()).toContain('4 done');
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
		expect(foldText()).toContain('4 done');
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
