import { tick } from 'svelte';
import { NEAR_BOTTOM_PX } from '$lib/nearBottom';

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
		this.setNearBottom(el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX);
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
			if (el) el.scrollTop = el.scrollHeight;
		});
	}

	captureBottomOffset(): void {
		const el = this.element();
		if (!el) return;
		this.#savedBottomOffset = Math.max(0, el.scrollHeight - el.scrollTop - el.clientHeight);
	}

	restoreBottomOffset(): void {
		this.beginProgrammaticScroll(() => {
			const el = this.element();
			if (!el) return;
			el.scrollTop = this.#savedBottomOffset === 0
				? el.scrollHeight
				: Math.max(0, el.scrollHeight - el.clientHeight - this.#savedBottomOffset);
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
