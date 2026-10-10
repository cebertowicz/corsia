import type { OperatorNode } from '$lib/api/plans';

/**
 * The fold wave: how finished rows leave the tree when they fold.
 *
 * Completion is the value-capture moment (finished-work journey), so every row that
 * folds gets the same small ceremony -- a check beat, a slide to its level's
 * fold row, a fold into that level's count. Rows leave one at a time in
 * on-screen order, each `STAGGER_MS` after the last, slowly enough to read;
 * the pane's row cap (`NEW_ROW_CAP`) bounds the wave, not a time cap. Reduced
 * motion skips the wave: the settled state is already rendered.
 *
 * The DOM holds the settled state from the first frame. Each folding row is a
 * cloned, inert *ghost* laid over the tree for the length of its animation,
 * and the rows that stay glide from where they were (FLIP), so nothing jumps.
 * The slide reads `--ease-ios-sheet`.
 */

export const FOLD_BEAT_MS = { check: 250, slide: 1000, fold: 450 } as const;
/** One row's whole animation. */
export const FOLD_ROW_MS = FOLD_BEAT_MS.check + FOLD_BEAT_MS.slide + FOLD_BEAT_MS.fold;
/** The gap between two rows' starts, whatever the count. */
export const STAGGER_MS = 550;

/**
 * Rows whose `matches` classification flipped from false to true since the
 * last render the user saw. Shared by the fold transition ("became folded",
 * which gets a ghost that flies into its level's count) and the sink
 * transition ("became finished", which only glides to its new place): both
 * play the same wave rather than a second animation system. With no earlier
 * render (first load, a reopened pane) nothing is newly true, so nothing
 * animates: replaying a parade of old history is not a transition anyone
 * watched.
 */
export function newlyMatching(
	previous: Map<string, boolean> | null,
	nodes: OperatorNode[],
	matches: (node: OperatorNode) => boolean,
): OperatorNode[] {
	if (previous === null) return [];
	return nodes.filter((node) => matches(node) && previous.get(node.id) === false);
}

/** How long new finished rows sit on screen, in a visible page, before they fold. */
export const PAUSE_MS = 1500;
/** The most new finished rows a view holds open; older new ones are already folded. */
export const NEW_ROW_CAP = 10;

/** A mark the user has never set lies before everything: nothing is behind it. */
function instant(value: string | null): number {
	return value === null ? -Infinity : Date.parse(value);
}

/** A held folded row renders under its folded ancestors, so they unfold with it. */
function withFoldedAncestors(ids: Iterable<string>, byId: Map<string, OperatorNode>): Set<string> {
	const held = new Set<string>();
	for (const id of ids) {
		let current = byId.get(id);
		while (current?.folded && !held.has(current.id)) {
			held.add(current.id);
			current = current.parent_id ? byId.get(current.parent_id) : undefined;
		}
	}
	return held;
}

/**
 * The folded rows the pane holds open: those that settled after
 * `seenThrough` (all of them in a plan never viewed), at most the
 * `NEW_ROW_CAP` most recently settled, with the folded ancestors they render
 * under. Every other folded row stays folded and never moves.
 */
export function heldRows(nodes: OperatorNode[], seenThrough: string | null): Set<string> {
	const seen = instant(seenThrough);
	const fresh = nodes
		.filter((node) => node.folded && Date.parse(node.last_transition_at) > seen)
		.sort((a, b) => Date.parse(b.last_transition_at) - Date.parse(a.last_transition_at))
		.slice(0, NEW_ROW_CAP);
	return withFoldedAncestors(fresh.map((node) => node.id), new Map(nodes.map((node) => [node.id, node])));
}

/** Rows that folded since the last render the user saw. */
export function newlyFolded(previous: Map<string, boolean> | null, nodes: OperatorNode[]): OperatorNode[] {
	return newlyMatching(previous, nodes, (node) => node.folded);
}

export function prefersReducedMotion(): boolean {
	return typeof window !== 'undefined'
		&& window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

interface Ghost { level: string; clone: HTMLElement; rect: DOMRect }

export interface FoldSnapshot {
	/** Where every row was before the fold, keyed by `data-flip-key`. */
	rects: Map<string, DOMRect>;
	/** The folding rows that were on screen, top to bottom. */
	ghosts: Ghost[];
}

function flipRows(container: HTMLElement): Map<string, HTMLElement> {
	const rows = new Map<string, HTMLElement>();
	for (const row of container.querySelectorAll<HTMLElement>('[data-flip-key]')) {
		rows.set(row.dataset.flipKey ?? '', row);
	}
	return rows;
}

/** Record the pre-fold layout and clone each folding row that is on screen. */
export function captureFold(
	container: HTMLElement,
	folding: { id: string; level: string }[],
): FoldSnapshot {
	const rows = flipRows(container);
	const rects = new Map([...rows].map(([key, row]) => [key, row.getBoundingClientRect()]));
	const levels = new Map(folding.map(({ id, level }) => [id, level]));
	const ghosts: Ghost[] = [];
	for (const [id, row] of rows) {
		const level = levels.get(id);
		if (level === undefined) continue;
		const clone = row.cloneNode(true) as HTMLElement;
		for (const element of [clone, ...clone.querySelectorAll('[id]')]) element.removeAttribute('id');
		clone.removeAttribute('data-flip-key');
		clone.setAttribute('aria-hidden', 'true');
		clone.inert = true;
		clone.classList.add('fold-ghost');
		ghosts.push({ level, clone, rect: rects.get(id)! });
	}
	ghosts.sort((a, b) => a.rect.top - b.rect.top);
	return { rects, ghosts };
}

function easing(container: HTMLElement): string {
	return getComputedStyle(container).getPropertyValue('--ease-ios-sheet').trim() || 'ease-out';
}

/** A computed colour's alpha: `rgba(…, a)`, `color(… / a)` or opaque. */
function alphaOf(fill: string): number {
	if (fill === 'transparent') return 0;
	const slash = fill.match(/\/\s*([\d.]+)(%?)\s*\)$/);
	const comma = fill.startsWith('rgba(') ? fill.match(/,\s*([\d.]+)\s*\)$/) : null;
	const found = slash ?? comma;
	if (!found) return 1;
	return slash?.[2] === '%' ? Number(found[1]) / 100 : Number(found[1]);
}

/**
 * The surface the tree is drawn on (a pane card, a drawer, an activity card),
 * so a travelling ghost covers the rows it passes in their own colour. Glass
 * surfaces are translucent layers over an opaque one; the ghost stacks the
 * same layers.
 */
function surfaceOf(element: HTMLElement): string {
	const layers: string[] = [];
	for (let node: HTMLElement | null = element; node; node = node.parentElement) {
		const fill = getComputedStyle(node).backgroundColor;
		const alpha = alphaOf(fill);
		if (alpha <= 0) continue;
		if (alpha >= 1) return [...layers.map((layer) => `linear-gradient(${layer}, ${layer})`), fill].join(', ');
		layers.push(fill);
	}
	return [...layers.map((layer) => `linear-gradient(${layer}, ${layer})`), 'var(--bg-primary)'].join(', ');
}

/** Glide every surviving row from its old place; a newly shown fold row fades in. */
function glide(container: HTMLElement, snapshot: FoldSnapshot, ease: string): Animation[] {
	const animations: Animation[] = [];
	for (const [key, row] of flipRows(container)) {
		const before = snapshot.rects.get(key);
		if (!before) {
			animations.push(row.animate([{ opacity: 0 }, { opacity: 1 }], { duration: FOLD_BEAT_MS.fold }));
			continue;
		}
		const dy = before.top - row.getBoundingClientRect().top;
		if (Math.abs(dy) < 0.5) continue;
		animations.push(row.animate(
			[{ transform: `translateY(${dy}px)` }, { transform: 'none' }],
			{ duration: FOLD_BEAT_MS.slide, delay: FOLD_BEAT_MS.check, easing: ease, fill: 'backwards' },
		));
	}
	return animations;
}

/** Send one ghost from its old place to its level's fold row, then fold it away. */
function sendGhost(container: HTMLElement, ghost: Ghost, delay: number, ease: string): Animation | null {
	const target = [...container.querySelectorAll<HTMLElement>('[data-fold-level]')]
		.find((row) => row.dataset.foldLevel === ghost.level);
	if (!target) return null;
	const origin = container.getBoundingClientRect();
	const top = ghost.rect.top - origin.top;
	const dy = target.getBoundingClientRect().top - origin.top - top;
	Object.assign(ghost.clone.style, {
		position: 'absolute', left: `${ghost.rect.left - origin.left}px`, top: `${top}px`,
		width: `${ghost.rect.width}px`, height: `${ghost.rect.height}px`, margin: '0',
		background: surfaceOf(container),
	});
	container.appendChild(ghost.clone);
	const at = (ms: number) => ms / FOLD_ROW_MS;
	ghost.clone.querySelector('.state-icon')?.animate(
		[{ transform: 'scale(1)' }, { transform: 'scale(1.25)' }, { transform: 'scale(1)' }],
		{ duration: FOLD_BEAT_MS.check, delay },
	);
	const animation = ghost.clone.animate([
		{ transform: 'translateY(0)', opacity: 1, offset: 0 },
		{ transform: 'translateY(0)', opacity: 1, offset: at(FOLD_BEAT_MS.check), easing: ease },
		{ transform: `translateY(${dy}px)`, opacity: 1, offset: at(FOLD_BEAT_MS.check + FOLD_BEAT_MS.slide) },
		{ transform: `translateY(${dy}px) scaleY(0.2)`, opacity: 0, offset: 1 },
	], { duration: FOLD_ROW_MS, delay, fill: 'both' });
	void animation.finished.then(() => ghost.clone.remove(), () => ghost.clone.remove());
	return animation;
}

/** One playing fold wave: `stop` removes every ghost; `done` settles when every animation has ended. */
export interface FoldPlay {
	stop: () => void;
	done: Promise<unknown>;
}

/** Play one fold wave over the settled DOM. */
export function playFold(container: HTMLElement, snapshot: FoldSnapshot): FoldPlay {
	const ease = easing(container);
	const animations = glide(container, snapshot, ease);
	snapshot.ghosts.forEach((ghost, index) => {
		const animation = sendGhost(container, ghost, index * STAGGER_MS, ease);
		if (animation) animations.push(animation);
	});
	return {
		stop: () => {
			for (const animation of animations) animation.cancel();
			for (const ghost of snapshot.ghosts) ghost.clone.remove();
		},
		done: Promise.allSettled(animations.map((animation) => animation.finished)),
	};
}
