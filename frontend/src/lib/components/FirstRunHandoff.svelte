<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import type { View } from '$lib/homePaneConfig';
	import { type FirstRunSelectionOwner } from '$lib/homeThreadSelection';
	import type { ThreadOpenSource } from '$lib/openTimingRuntime';
	import type { UiScope } from '$lib/stores/uiScope';
	import type { CoordinationLookup } from '$lib/utils/conductor';
	import { createFirstRunHandoff } from '$lib/firstRunHandoff';

	let {
		authenticated, token, scope, selectionIntent, setupFinishedHandoff, setupFinishedIntent, chatPanelReady, activeThread, activeView, selectThread,
		conductorLookup = $bindable<CoordinationLookup>({ kind: 'unknown' }),
	}: {
		authenticated: boolean;
		token: string | null;
		scope: UiScope | null;
		selectionIntent: number;
		setupFinishedHandoff: number;
		setupFinishedIntent: number | null;
		chatPanelReady: boolean;
		activeThread: string | null;
		activeView: View;
		selectThread: (id: string, source?: ThreadOpenSource, firstRunOwner?: FirstRunSelectionOwner) => Promise<void>;
		conductorLookup?: CoordinationLookup;
	} = $props();

	const handoff = createFirstRunHandoff();
	let mounted = true;
	onDestroy(() => { mounted = false; });
	let lastLookupKey: string | null = null;
	const current = () => ({ authenticated, token, scope, selectionIntent, activeThread });
	async function open(id: string, owner: () => boolean) {
		if (!mounted || activeView === 'measurement') return false;
		await selectThread(id, 'other', () => mounted && owner());
		return mounted && chatPanelReady;
	}

	$effect(() => {
		const [lookupToken, lookupScope, lookupAuthenticated] = [token, scope, authenticated];
		const lookupKey = lookupAuthenticated ? `${lookupToken}\u0000${lookupScope?.id ?? ''}` : '';
		if (lookupKey === lastLookupKey) return;
		lastLookupKey = lookupKey;
		conductorLookup = { kind: 'unknown' };
		if (!lookupAuthenticated) return;
		let active = true;
		untrack(() => handoff.startLookup({
			token: lookupToken, scope: lookupScope, selectionIntent, isActive: () => active, current,
			onLookup: (lookup) => { conductorLookup = lookup; }, open,
		}));
		return () => { active = false; };
	});

	$effect(() => {
		if (!setupFinishedHandoff) return;
		if (setupFinishedIntent === null) return;
		handoff.setupFinished(setupFinishedIntent);
		return handoff.landAfterSetup({ panelReady: chatPanelReady, activeThread, current, open });
	});
</script>
