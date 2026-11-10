/**
 * Which of a thread's tabs the PDF viewer shows. A file the backend says is a PDF is shown the same way an image is, by its own
 * viewer; what the tab's load found about it is kept here, not on the tab, so a file replaced by one of another kind is shown by
 * whichever viewer the next load finds it needs. Call it while a component initialises: it forgets tabs that were closed.
 */
export function createPdfTabs(threadId: () => string | null | undefined, tabs: () => readonly { instanceId: string }[]) {
	let keys = $state<string[]>([]);
	const keyOf = (instanceId: string) => `${threadId()}\u0000${instanceId}`;
	$effect(() => {
		const open = new Set(tabs().map((t) => keyOf(t.instanceId)));
		const prefix = `${threadId()}\u0000`;
		const kept = keys.filter((k) => !k.startsWith(prefix) || open.has(k));
		if (kept.length !== keys.length) keys = kept;
	});
	return {
		has: (instanceId: string) => keys.includes(keyOf(instanceId)),
		/** Records what a load found: this tab's file is, or is no longer, a PDF. */
		mark(instanceId: string, isPdf: boolean) {
			const key = keyOf(instanceId);
			const others = keys.filter((k) => k !== key);
			keys = isPdf ? [...others, key] : others;
		},
	};
}
