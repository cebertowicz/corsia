/**
 * Where each comment thread lives (the chat or the document it belongs to), by its root id.
 *
 * A thread's read can be sent after the surface showing it has moved on: its card closes as the person switches to another
 * chat or document, and the surface is not rebuilt for the new one. The read still belongs to the thread's own chat or
 * document, so the surface notes each thread's home while it shows it and the stamp sends there, never to whatever is on
 * screen at that moment. The first home noted for a root id stands: a thread never moves.
 * A thread with no home noted (never shown here) has nowhere to be read, so its stamp sends nothing.
 */
export function createThreadHomes() {
	const homes = new Map<string, string>();
	return {
		note(rootIds: Iterable<string>, home: string | null | undefined): void {
			if (!home) return;
			for (const id of rootIds) if (!homes.has(id)) homes.set(id, home);
		},
		of(rootId: string): string | undefined {
			return homes.get(rootId);
		},
		/** Sends a thread's stamp to its home; a thread with none noted sends nothing. */
		stamp(rootId: string, send: (home: string) => Promise<void>): Promise<void> {
			const home = homes.get(rootId);
			return home ? send(home) : Promise.resolve();
		},
	};
}
