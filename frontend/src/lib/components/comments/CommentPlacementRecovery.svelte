<script lang="ts">
	import { tick } from 'svelte';
	import { Button } from '../ui';
	import { footerFocusHome } from '$lib/docCommentDrawer';

	export type PlacementRecoveryProps = {
		onReload?: () => boolean;
		reloading?: boolean;
		reloadError?: boolean;
		unavailable?: boolean;
		connectionDescriptionId?: string;
		orphanedCount: number;
	};
	let { orphanedCount, onReload, reloading = false, reloadError = false, unavailable = false, connectionDescriptionId, onFocusHome }: PlacementRecoveryProps & {
		onFocusHome: () => HTMLElement | undefined;
	} = $props();
	const unplacedPhrase = $derived(`${orphanedCount} comment${orphanedCount === 1 ? '' : 's'} couldn't be placed`);
	// A refusal is stored with the comments, so it remains visible when the drawer is closed and reopened.
	const recoveryText = $derived(reloadError ? "Couldn't reload comment placements" : announcement || unplacedPhrase);
	let requested = $state(false);
	let sawBusy = $state(false);
	let announcement = $state('');
	let announcementVersion = $state(0);
	let announcedCount = $state<number | null>(null);
	let placementFooter = $state<HTMLElement>();
	$effect(() => {
		if (announcement && announcedCount !== orphanedCount) {
			announcement = '';
			announcedCount = null;
		}
	});

	$effect(() => {
		if (!requested) return;
		if (reloading) { sawBusy = true; return; }
		if (!sawBusy) return;
		requested = false;
		sawBusy = false;
		announcement = reloadError ? "Couldn't reload comment placements" : orphanedCount ? `${orphanedCount} comment${orphanedCount === 1 ? '' : 's'} still couldn't be placed` : 'Comments placed';
		announcementVersion += 1;
		announcedCount = orphanedCount;
		// The loading button may be replaced while its request settles; restore focus to the surviving recovery action.
		if (orphanedCount) void tick().then(() => placementFooter?.querySelector<HTMLButtonElement>('[aria-label="Reload comment placements"]')?.focus());
		else void tick().then(() => onFocusHome()?.focus());
	});

	function reload(): void {
		if (!onReload || unavailable || reloading) return;
		requested = onReload();
	}
</script>

{#if (orphanedCount || announcement) && onReload}
	<div class="drawer-placement" data-drawer-placement bind:this={placementFooter} use:footerFocusHome={onFocusHome}>
		{#key `${announcementVersion}:${reloadError}`}<p role="status">{recoveryText}</p>{/key}
		{#if orphanedCount}<Button size="sm" variant="secondary" ariaLabel="Reload comment placements" ariaDisabled={unavailable || reloading} describedby={unavailable ? connectionDescriptionId : undefined} onclick={reload} loading={reloading}>Place again</Button>{/if}
	</div>
{/if}

<style>
	.drawer-placement { display: flex; align-items: center; gap: var(--space-2); margin: 0; padding: var(--space-2) var(--space-3); border-top: 1px solid var(--border-subtle); border-radius: 0 0 calc(var(--radius-lg) - 1px) calc(var(--radius-lg) - 1px); background: var(--bg-secondary); }
	.drawer-placement p { flex: 1; min-width: 0; margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.drawer-placement :global(.btn) { flex: none; }
	@media (max-width: 420px) { .drawer-placement { gap: var(--space-1); padding-inline: var(--space-2); } }
	@media (pointer: coarse) { .drawer-placement :global(.btn) { min-height: 44px; } }
</style>
