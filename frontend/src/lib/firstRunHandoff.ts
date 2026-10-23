import type { UiScope } from '$lib/stores/uiScope';
import { fetchScopeTarget, landOnFirstRun, type CoordinationLookup } from '$lib/utils/conductor';

type CurrentFirstRunState = {
	authenticated: boolean;
	token: string | null;
	scope: UiScope | null;
	selectionIntent: number;
	activeThread: string | null;
};

type FirstRunOpen = (id: string, ownsSelection: () => boolean) => Promise<boolean>;

/** Holds an incomplete setup-time landing until ChatPanel exists after setup. */
export function createFirstRunHandoff() {
	let latestLookup: { lookup: CoordinationLookup; scope: UiScope | null } | null = null;
	let setupFinishedIntent: number | null = null;

	function ownsSelection(
		isActive: () => boolean,
		firstRunIntent: number,
		initialToken: string | null,
		initialScope: UiScope | null,
		current: () => CurrentFirstRunState,
	) {
		return () => {
			const state = current();
			return isActive() && state.authenticated && state.token === initialToken && state.scope === initialScope
				&& state.selectionIntent === firstRunIntent;
		};
	}

	return {
		startLookup({
			token, scope, selectionIntent, isActive, current, onLookup, open,
		}: {
			token: string | null;
			scope: UiScope | null;
			selectionIntent: number;
			isActive: () => boolean;
			current: () => CurrentFirstRunState;
			onLookup: (lookup: CoordinationLookup) => void;
			open: FirstRunOpen;
		}) {
			void fetchScopeTarget(token, scope).then((lookup) => {
				if (!isActive()) return;
				onLookup(lookup);
				latestLookup = { lookup, scope };
				void landOnFirstRun(lookup, scope, (id) => open(id, ownsSelection(isActive, selectionIntent, token, scope, current)));
			});
		},
		setupFinished(selectionIntent: number) {
			setupFinishedIntent = selectionIntent;
		},
		landAfterSetup({
			panelReady, activeThread, current, open,
		}: {
			panelReady: boolean;
			activeThread: string | null;
			current: () => CurrentFirstRunState;
			open: FirstRunOpen;
		}) {
			const state = current();
			const cached = latestLookup;
			if (!panelReady || activeThread || setupFinishedIntent !== state.selectionIntent || !state.authenticated
				|| !cached || cached.scope !== state.scope || cached.lookup.kind !== 'target' || !cached.lookup.firstRunPending) return;
			let active = true;
			let openingId: string | undefined;
			void landOnFirstRun(cached.lookup, cached.scope, (id) => {
				openingId = id;
				return open(id, ownsSelection(() => active, state.selectionIntent, state.token, state.scope, current));
			});
			return () => {
				// A reactive active-thread update can be this landing's own selection.
				if (openingId === undefined || current().activeThread !== openingId) active = false;
			};
		},
	};
}
