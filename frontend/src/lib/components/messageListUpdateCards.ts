import type { UpdateCard } from '$lib/api/threadUpdates';

export type DividerLanding = { threadId: string; generation: number };

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
export class DividerLandingIntent {
	#landing: DividerLanding | null = null;
	#generation = 0;

	constructor(private readonly onSettled: (threadId: string) => void) {}

	begin(threadId: string): DividerLanding {
		const landing = { threadId, generation: ++this.#generation };
		this.#landing = landing;
		return landing;
	}

	isCurrent(landing: DividerLanding): boolean {
		return this.#landing?.threadId === landing.threadId
			&& this.#landing.generation === landing.generation;
	}

	blocksLiveEdge(threadId: string | null | undefined): boolean {
		return this.#landing?.threadId === threadId;
	}

	cancel(threadId?: string): void {
		if (!this.#landing || (threadId && this.#landing.threadId !== threadId)) return;
		const { threadId: settledThreadId } = this.#landing;
		this.#generation++;
		this.#landing = null;
		this.onSettled(settledThreadId);
	}

	finish(landing: DividerLanding): void {
		if (!this.isCurrent(landing)) return;
		this.#landing = null;
		this.onSettled(landing.threadId);
	}
}

/** Keep divider intent through the programmatic-scroll release frame. */
export function scheduleDividerLanding(
	landing: DividerLanding,
	intent: DividerLandingIntent,
	afterTick: () => Promise<void>,
	frame: Frame,
	beginProgrammaticScroll: BeginProgrammaticScroll,
	writeDivider: () => void,
): void {
	afterTick().then(() => frame(() => {
		if (!intent.isCurrent(landing)) return;
		beginProgrammaticScroll(() => {
			if (intent.isCurrent(landing)) writeDivider();
		});
		frame(() => intent.finish(landing));
	}));
}
