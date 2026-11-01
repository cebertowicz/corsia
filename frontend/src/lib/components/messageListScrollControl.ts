import { tick } from 'svelte';
import { NEAR_BOTTOM_PX } from '$lib/nearBottom';
import { contentEnd } from '$lib/actions/holdTail';

/** The scrollTop that leaves `offset` px of content below the view, measured to where the content ends (not the floor). */
function bottomTop(el: HTMLElement, offset: number): number {
	return Math.max(0, contentEnd(el) - el.clientHeight - offset);
}

/** Keeps transcript scrolling separate from the component's rendering and landing state. */
export class MessageListScrollControl {
	#pendingReleaseFrame: number | null = null;
	#userScrollEpoch = 0;
	#savedBottomOffset = 0;

	constructor(
		private readonly element: () => HTMLElement | undefined,
		private readonly setNearBottom: (nearBottom: boolean) => void,
		private readonly setIgnoringScroll: (ignoring: boolean) => void,
	) {}

	get userScrollEpoch(): number { return this.#userScrollEpoch; }

	noteUserScroll(): void {
		this.#userScrollEpoch++;
		this.endProgrammaticScroll();
		this.syncNearBottom();
	}

	syncNearBottom(): void {
		const el = this.element();
		if (!el) return;
		this.setNearBottom(contentEnd(el) - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX);
	}

	beginProgrammaticScroll(write: () => void): void {
		if (!this.element()) return;
		this.setIgnoringScroll(true);
		write();
		if (this.#pendingReleaseFrame !== null) cancelAnimationFrame(this.#pendingReleaseFrame);
		this.#pendingReleaseFrame = requestAnimationFrame(() => {
			this.#pendingReleaseFrame = null;
			this.setIgnoringScroll(false);
			this.syncNearBottom();
		});
	}

	requestBottom(blocked: () => boolean, follow: () => void): void {
		if (blocked()) return;
		const epoch = this.#userScrollEpoch;
		tick().then(() => {
			if (epoch !== this.#userScrollEpoch || blocked()) return;
			follow();
		});
	}

	writeBottom(): void {
		this.beginProgrammaticScroll(() => {
			const el = this.element();
			if (!el) return;
			// Never back up: a live edge that got shorter leaves the blank floor below, and the text stays put.
			const floored = contentEnd(el) < el.scrollHeight;
			el.scrollTop = floored ? Math.max(el.scrollTop, bottomTop(el, 0)) : el.scrollHeight;
		});
	}

	/** The band under the transcript got `by` px shorter under a pinned reader: the view comes down with it to the content's end, and no further. */
	shedBand(by: number): void {
		const el = this.element();
		if (!el) return;
		const end = bottomTop(el, 0);
		if (el.scrollTop <= end) return;
		this.beginProgrammaticScroll(() => { el.scrollTop = Math.max(end, el.scrollTop - by); });
	}

	captureBottomOffset(): void {
		const el = this.element();
		if (!el) return;
		// Signed: blank space held below the content, in view, is a place to come back to as well.
		this.#savedBottomOffset = contentEnd(el) - el.scrollTop - el.clientHeight;
	}

	restoreBottomOffset(): void {
		this.beginProgrammaticScroll(() => {
			const el = this.element();
			if (!el) return;
			el.scrollTop = this.#savedBottomOffset <= 0 && contentEnd(el) >= el.scrollHeight ? el.scrollHeight : bottomTop(el, this.#savedBottomOffset);
		});
	}

	dispose(): void {
		this.endProgrammaticScroll();
	}

	private endProgrammaticScroll(): void {
		if (this.#pendingReleaseFrame !== null) {
			cancelAnimationFrame(this.#pendingReleaseFrame);
			this.#pendingReleaseFrame = null;
		}
		this.setIgnoringScroll(false);
	}
}
