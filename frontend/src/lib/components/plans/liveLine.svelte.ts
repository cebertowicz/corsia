import { advanceLiveLine, NO_LIVE_LINE, type LiveStep } from './planLiveStep';

/**
 * The single reactive owner for the line a running surface shows. It preserves
 * the final phrase briefly across a Turn boundary and clears it when its owner
 * reaches a terminal state. `segment` is passed through to `advanceLiveLine`.
 */
export function createLiveLine(
	live: () => LiveStep | null,
	ended: () => boolean,
	segment?: () => string | undefined,
): { readonly shown: LiveStep | null } {
	let line = NO_LIVE_LINE;
	let tick = $state(0);
	const shown = $derived.by(() => {
		void tick;
		const next = advanceLiveLine(line, live(), ended(), Date.now(), segment?.());
		line = next.line;
		return next.show;
	});
	$effect(() => {
		if (!shown || live() || line.deadline === null) return;
		const timer = setTimeout(() => { tick += 1; }, Math.max(0, line.deadline - Date.now()));
		return () => clearTimeout(timer);
	});
	return { get shown() { return shown; } };
}
