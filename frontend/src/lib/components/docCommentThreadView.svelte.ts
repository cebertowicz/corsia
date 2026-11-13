import { tick } from 'svelte';

/**
 * The comment list's thread view: one thread whole in the same sheet or drawer, with Back to the list where it was.
 * Opening keeps the list's scroll offset and starts the thread at the top with focus on Back; Back puts the offset
 * back and returns focus to the row's open control.
 */
export function createListThreadView(drawer: () => HTMLElement | undefined) {
	let opened = $state<string | null>(null);
	let listTop = 0;
	const list = () => drawer()?.querySelector<HTMLElement>('.drawer-comments') ?? null;
	return {
		get id() { return opened; },
		open(id: string) {
			listTop = list()?.scrollTop ?? 0;
			opened = id;
			void tick().then(() => {
				const el = list();
				if (el) el.scrollTop = 0;
				drawer()?.querySelector<HTMLElement>('[data-drawer-back]')?.focus({ preventScroll: true });
			});
		},
		back() {
			const id = opened;
			opened = null;
			void tick().then(() => {
				const el = list();
				if (el) el.scrollTop = listTop;
				drawer()?.querySelector<HTMLElement>(`[data-comment-id="${id}"] .ct-open-thread`)?.focus({ preventScroll: true });
			});
		},
		reset() { opened = null; },
	};
}
