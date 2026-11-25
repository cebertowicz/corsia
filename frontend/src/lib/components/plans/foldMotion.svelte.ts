/**
 * How finished rows move in `PlanTree`: holding what is new since the user
 * last looked at the plan in the pane, and the fold wave that carries rows
 * into their level's count. Which rows are new is the server's word: a pane
 * read lists the folded rows since the user's last look (`plans.open_tree`'s
 * fold window), and the client holds those open until it has shown them. The
 * animation itself lives in `foldWave.ts`; this owns the tree's motion state.
 * Call both during component initialisation.
 */
import type { OperatorNode } from '$lib/api/plans';
import {
	captureFold, newlyFolded, newlyLater, newlyMatching, playFold, prefersReducedMotion, type FoldSnapshot,
} from './foldWave';

/** One pane view of a plan: the folded rows the pane has already shown fold (the rest are new to the user). */
export interface PaneView {
	released: ReadonlySet<string>;
}

const NONE: ReadonlySet<string> = new Set();

/** The folded rows a read lists that this view has not yet shown fold: new to the user, held open. */
export function heldIds(nodes: OperatorNode[], view: PaneView | null): Set<string> {
	if (view === null) return new Set();
	return new Set(nodes.filter((node) => node.folded && !view.released.has(node.id)).map((node) => node.id));
}

/**
 * The tree's input with the pane's held rows unfolded; when the pane releases
 * them they fold in the ordinary wave. Without a pane view (an Activity card,
 * a drill-down) the tree shows the server's folds as they are.
 */
export function createPaneHold(getNodes: () => OperatorNode[], getView: () => PaneView | null) {
	const held = $derived(heldIds(getNodes(), getView()));
	const nodes = $derived(
		held.size === 0 ? getNodes() : getNodes().map((node) => (held.has(node.id) ? { ...node, folded: false } : node)),
	);
	return { get nodes() { return nodes; }, get held(): ReadonlySet<string> { return held.size === 0 ? NONE : held; } };
}

export interface FoldWaveInput {
	nodes: () => OperatorNode[];
	/** The rendered items; the wave plays once the DOM holds them. */
	items: () => unknown;
	element: () => HTMLElement | null;
	isFinished: (node: OperatorNode) => boolean;
	/** True when a node has no row rendered for it: a Later member of a collapsed group. */
	isHidden: (node: OperatorNode) => boolean;
	/** A folding row's flip key and fold level, as the tree renders them. */
	keyOf: (node: OperatorNode) => { id: string; level: string };
}

/**
 * The fold wave: before the DOM takes a new snapshot, note which on-screen
 * rows are about to fold, sink to their finished group, or cross into or out of
 * the Later group; after it, play them -- a fold gets a ghost into its level's
 * count, a crossing into a collapsed group gets one into the group's own row,
 * a sink or a crossing you can see the end of only glides to its new place,
 * all the same wave. The first render only records what already
 * folded, was already finished or was already Later. Neither a refresh nor a later wave (a row
 * finishing mid-wave) cuts a wave short; waves stop only when the tree unmounts,
 * and a wave that has ended lets go of its stop.
 */
export function createFoldWave(input: FoldWaveInput): void {
	let seenFolded: Map<string, boolean> | null = null;
	let seenFinished: Map<string, boolean> | null = null;
	let seenLater: Map<string, boolean> | null = null;
	let pendingFold: FoldSnapshot | null = null;
	$effect.pre(() => {
		const nodes = input.nodes();
		const folding = newlyFolded(seenFolded, nodes);
		const sinking = newlyMatching(seenFinished, nodes, input.isFinished);
		// A Later crossing you can see the end of is not a removal: the row stays on
		// screen at its new place, so it arms the wave like a sinking row and nothing
		// ghosts it. Into a collapsed group there is no row to glide to, so the leaving
		// row takes the wave's ghost into the group row, as a folding row takes one
		// into its level's count -- the count changes in place while it collapses there.
		const crossing = newlyLater(seenLater, nodes);
		const hidden = crossing.filter((node) => node.later === true && input.isHidden(node));
		seenFolded = new Map(nodes.map((node) => [node.id, node.folded]));
		seenFinished = new Map(nodes.map((node) => [node.id, input.isFinished(node)]));
		seenLater = new Map(nodes.map((node) => [node.id, node.later === true]));
		const element = input.element();
		const still = folding.length === 0 && sinking.length === 0 && crossing.length === 0;
		if (still || !element || prefersReducedMotion()) return;
		pendingFold = captureFold(element, [
			...folding.map(input.keyOf),
			// The group row is a different bucket of the same level, so its ghost asks
			// for the level's Later target rather than the finished-count one.
			...hidden.map((node) => {
				const key = input.keyOf(node);
				return { id: key.id, level: `${key.level}:later` };
			}),
		]);
	});
	const stops = new Set<() => void>();
	$effect(() => {
		void input.items();
		const element = input.element();
		if (!pendingFold || !element) return;
		const snapshot = pendingFold;
		pendingFold = null;
		const wave = playFold(element, snapshot);
		stops.add(wave.stop);
		void wave.done.then(() => stops.delete(wave.stop));
	});
	$effect(() => () => { for (const stop of stops) stop(); });
}
