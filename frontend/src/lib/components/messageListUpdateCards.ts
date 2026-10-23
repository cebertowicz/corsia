import type { UpdateCard } from '$lib/api/threadUpdates';

export type InitialLanding = { threadId: string; generation: number; target: 'divider' | 'opening' };

type Frame = (callback: FrameRequestCallback) => number;
type BeginProgrammaticScroll = (write: () => void) => void;

/** Tracks asynchronous card references without owning any transcript behavior. */
export class RenderedUpdateCards {
	#threadId: string | null = null;
	#cards: UpdateCard[] | undefined;

	changed(threadId: string | null, cards: UpdateCard[] | undefined): boolean {
		const changed = threadId !== this.#threadId || cards !== this.#cards;
		this.#threadId = threadId;
		this.#cards = cards;
		return changed;
	}
}

/**
 * Transient update-dot landing intent. Its owner supplies settlement so this
 * helper never reads shared stores, touches the DOM, or writes scroll position.
 */
export class InitialLandingIntent {
	#landing: InitialLanding | null = null;
	#generation = 0;

	constructor(private readonly onSettled: (landing: InitialLanding) => void) {}

	begin(threadId: string, target: InitialLanding['target']): InitialLanding {
		const landing = { threadId, target, generation: ++this.#generation };
		this.#landing = landing;
		return landing;
	}

	isCurrent(landing: InitialLanding): boolean {
		return this.#landing?.threadId === landing.threadId
			&& this.#landing.generation === landing.generation;
	}

	blocksLiveEdge(threadId: string | null | undefined): boolean {
		return this.#landing?.threadId === threadId;
	}

	targetFor(threadId: string | null | undefined): InitialLanding['target'] | null {
		const landing = this.#landing;
		if (!landing || landing.threadId !== threadId) return null;
		return landing.target;
	}

	finishCurrent(threadId: string, target: InitialLanding['target']): void {
		if (this.#landing?.threadId === threadId && this.#landing.target === target) this.finish(this.#landing);
	}

	cancel(threadId?: string): void {
		if (!this.#landing || (threadId && this.#landing.threadId !== threadId)) return;
		const landing = this.#landing;
		this.#generation++;
		this.#landing = null;
		this.onSettled(landing);
	}

	finish(landing: InitialLanding): void {
		if (!this.isCurrent(landing)) return;
		this.#landing = null;
		this.onSettled(landing);
	}
}

/** Keep an initial landing intent through the programmatic-scroll release frame. */
export function scheduleInitialLanding(
	landing: InitialLanding,
	intent: InitialLandingIntent,
	shouldLand: () => boolean,
	holdLanding: () => boolean,
	afterTick: () => Promise<void>,
	frame: Frame,
	beginProgrammaticScroll: BeginProgrammaticScroll,
	writeLanding: () => void,
): void {
	afterTick().then(() => frame(() => {
		if (!intent.isCurrent(landing)) return;
		if (!shouldLand()) {
			intent.cancel(landing.threadId);
			return;
		}
		beginProgrammaticScroll(() => {
			if (intent.isCurrent(landing) && shouldLand()) writeLanding();
		});
		if (!holdLanding()) frame(() => intent.finish(landing));
	}));
}
