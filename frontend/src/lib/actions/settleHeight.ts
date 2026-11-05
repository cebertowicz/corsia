/** Content a panel is told eases its own height, with the height it had at the panel's last look (null before the first one). */
type Carried = { holds: number; height: number | null };
type Panel = { held: Map<HTMLElement, Carried>; watch: (content: HTMLElement, on: boolean) => void };
const carrying = new WeakMap<HTMLElement, Panel>();

/**
 * Content that eases its own height tells the panel around it, because a frame that drops as the ease ends can run it to
 * its end between two looks: the panel then sees a snap it never saw easing, and replays it. The hold lasts until the
 * frame after the one that laid out the end height, however late that frame lands. The hold stops the replay of the
 * content's own change only: the panel measures that content too, so a resize the content did not make, landing during the
 * hold, still eases.
 */
export function carryPanelHeight(content: HTMLElement, settled: Promise<unknown>) {
	for (let panel = content.parentElement; panel; panel = panel.parentElement) {
		const owner = carrying.get(panel);
		if (!owner) continue;
		const entry = owner.held.get(content) ?? { holds: 0, height: null };
		entry.holds++;
		owner.held.set(content, entry);
		owner.watch(content, true);
		const release = () => {
			entry.holds = Math.max(0, entry.holds - 1);
			if (entry.holds === 0 && owner.held.get(content) === entry) { owner.held.delete(content); owner.watch(content, false); }
		};
		void settled.catch(() => {}).then(() => requestAnimationFrame(() => requestAnimationFrame(release)));
		return;
	}
}

/**
 * Eases a bottom-anchored panel from its old height to its new one when its content changes the height
 * (a filter that leaves fewer rows, a footer appearing), instead of snapping in a single frame.
 *
 * Only a change nothing else is easing is eased: a detent move already transitions `max-height`, content that
 * grows through its own height animation (a slide, a textarea) carries the panel with it frame by frame, and a drag
 * is the user's own motion. With `enabled` false (a drag) the change is a cut. With `reduced` the size change cuts
 * and what is new in the panel fades in over `--dur-tight` instead: reduced motion crossfades, nothing slides.
 */
export function settleHeight(node: HTMLElement, options: { enabled: boolean; reduced?: boolean }) {
	let enabled = options.enabled;
	let reduced = options.reduced ?? false;
	const held = new Map<HTMLElement, Carried>();
	carrying.set(node, { held, watch: (content, on) => (on ? observer?.observe(content) : observer?.unobserve(content)) });
	let last = node.getBoundingClientRect().height;
	let animation: Animation | null = null;
	let destroyed = false;
	let detentMoves = 0;
	const border = () => {
		const style = getComputedStyle(node);
		return style.boxSizing === 'border-box' ? 0 : (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
	};
	/** Content that animates its own height already carries the panel with it, frame by frame. */
	const contentEases = () => (typeof node.getAnimations === 'function' && node.getAnimations({ subtree: true }).some((item) =>
		item !== animation && item.playState === 'running' && (item.effect as KeyframeEffect | null)?.getKeyframes?.().some((frame) => 'height' in frame)));
	/**
	 * What of the panel's `change` the held content did not make: the content's height is read at each look, so the part of
	 * the change it made itself drops out. Nothing is known about a content until its first look, so that look is all its own.
	 */
	const unexplained = (change: number) => {
		let own = 0, known = held.size > 0;
		for (const [content, entry] of held) {
			const height = content.getBoundingClientRect().height;
			if (entry.height === null) known = false;
			else own += height - entry.height;
			entry.height = height;
		}
		return known ? change - own : 0;
	};
	const fadeIn = () => {
		const style = getComputedStyle(node);
		const duration = Number.parseFloat(style.getPropertyValue('--dur-tight')) || 120;
		for (const child of Array.from(node.children)) {
			if (child instanceof HTMLElement && !child.classList.contains('drawer-chrome')) child.animate([{ opacity: 0 }, { opacity: 1 }], { duration, easing: style.getPropertyValue('--ease-snap') || 'ease-out' });
		}
	};
	const run = (from: number, to: number) => {
		// The panel's own animation changes its size; observe it again once that animation settles.
		observer?.unobserve(node);
		const inset = border();
		// `fill: both` holds the end height from the frame the ease ends until `done` has handled the finish, which can
		// land a frame or two later: without it those frames show the content's own height, a snap that is then eased back.
		const current = node.animate(
			[{ height: `${from - inset}px` }, { height: `${to - inset}px` }],
			{ duration: 200, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', fill: 'both' },
		);
		animation = current;
		const done = () => {
			animation = null;
			// Released and read in one task, so no frame shows the height the content alone gives.
			current.cancel();
			// Content may have changed again while this ran; ease on from where it stopped.
			const now = node.getBoundingClientRect().height;
			last = now;
			if (enabled && Math.abs(now - to) > 1) run(to, now);
			else if (!destroyed) observer?.observe(node);
		};
		current.addEventListener('finish', done, { once: true });
		current.addEventListener('cancel', () => {
			if (animation !== current) return;
			animation = null;
			last = node.getBoundingClientRect().height;
			if (!destroyed) observer?.observe(node);
		}, { once: true });
	};
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
		const now = node.getBoundingClientRect().height;
		const rest = unexplained(now - last);
		if (animation || detentMoves > 0) return;
		const from = last;
		last = now;
		if (!enabled || typeof node.animate !== 'function' || Math.abs(now - from) <= 1) return;
		// Under a hold only what the held content did not make eases, from where that part began.
		if (held.size > 0 ? Math.abs(rest) <= 1 : contentEases()) return;
		if (reduced) fadeIn();
		else run(held.size > 0 ? now - rest : from, now);
	});
	const moveStart = (event: TransitionEvent) => { if (event.target === node && event.propertyName === 'max-height') detentMoves++; };
	const moveEnd = (event: TransitionEvent) => {
		if (event.target !== node || event.propertyName !== 'max-height') return;
		detentMoves = Math.max(0, detentMoves - 1);
		if (detentMoves === 0) last = node.getBoundingClientRect().height;
	};
	node.addEventListener('transitionrun', moveStart);
	node.addEventListener('transitionend', moveEnd);
	node.addEventListener('transitioncancel', moveEnd);
	observer?.observe(node);
	return {
		update(next: { enabled: boolean; reduced?: boolean }) { enabled = next.enabled; reduced = next.reduced ?? false; },
		destroy() {
			destroyed = true;
			observer?.disconnect();
			carrying.delete(node);
			animation?.cancel();
			node.removeEventListener('transitionrun', moveStart);
			node.removeEventListener('transitionend', moveEnd);
			node.removeEventListener('transitioncancel', moveEnd);
		},
	};
}
