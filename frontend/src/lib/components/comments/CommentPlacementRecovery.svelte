<script lang="ts">
	import { tick } from 'svelte';
	import { Button } from '../ui';
	import type { DocComment } from '$lib/stores/docComments';
	import { drawerFooterHeight, footerFocusHome } from '$lib/docCommentDrawer';

	export type PlacementRecoveryProps = {
		onReload?: () => boolean;
		reloading?: boolean;
		reloadError?: boolean;
		unavailable?: boolean;
		connectionDescriptionId?: string;
	};
	let { comments, onReload, reloading = false, reloadError = false, unavailable = false, connectionDescriptionId, onFocusHome }: {
		comments: DocComment[];
		onFocusHome: () => HTMLElement | undefined;
	} & PlacementRecoveryProps = $props();

	const orphanedCount = $derived(comments.filter((comment) => comment.orphaned && !comment.resolved).length);
	const unplacedPhrase = $derived(`${orphanedCount} comment${orphanedCount === 1 ? '' : 's'} couldn't be placed`);
	let requested = $state(false);
	let sawBusy = $state(false);
	let announcement = $state('');
	let placementFooter = $state<HTMLElement>();

	$effect(() => {
		if (!requested) return;
		if (reloading) { sawBusy = true; return; }
		if (!sawBusy) return;
		requested = false;
		sawBusy = false;
		announcement = reloadError ? "Couldn't reload comment placements" : orphanedCount ? `${orphanedCount} comment${orphanedCount === 1 ? '' : 's'} still couldn't be placed` : 'Comments placed';
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
	<div class="drawer-placement" data-drawer-placement bind:this={placementFooter} use:drawerFooterHeight use:footerFocusHome={onFocusHome}>
		<p role="status">{announcement || unplacedPhrase}</p>
		{#if orphanedCount}<Button size="sm" variant="secondary" ariaLabel="Reload comment placements" ariaDisabled={unavailable || reloading} describedby={unavailable ? connectionDescriptionId : undefined} onclick={reload} loading={reloading}>Place again</Button>{/if}
	</div>
{/if}

<style>
	.drawer-placement { container: drawer-footer / inline-size; flex: none; display: flex; align-items: center; gap: var(--space-2); margin: 0; padding: var(--space-2) var(--space-3); border-top: 1px solid var(--border-subtle); border-radius: 0 0 calc(var(--radius-lg) - 1px) calc(var(--radius-lg) - 1px); background: var(--bg-secondary); }
	.drawer-placement p { flex: 1; min-width: 0; margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.drawer-placement :global(.btn) { flex: none; }
	@media (max-width: 420px) { .drawer-placement { gap: var(--space-1); padding-inline: var(--space-2); } }
	@media (pointer: coarse) { .drawer-placement :global(.btn) { min-height: 44px; } }
</style>
