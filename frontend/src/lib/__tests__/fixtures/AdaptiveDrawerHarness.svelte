<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';

	let { onClose, tall = false, fill = false, followKeyboard = false, modal = true, detent = 'full', callerControlled = false, keepMounted = false, covered = false, contextLabel = null, draft = false, head = null, onDetent }: { head?: (() => void) | null; onDetent?: (next: 'full' | 'half') => void; draft?: boolean; onClose: () => void; tall?: boolean; fill?: boolean; followKeyboard?: boolean; modal?: boolean; detent?: 'full' | 'half'; callerControlled?: boolean; keepMounted?: boolean; covered?: boolean; contextLabel?: string | null } = $props();
	let surfaceOpen = $state(false);
	let drawerOpen = $state(false);

	function openDrawer(): void {
		surfaceOpen = true;
		drawerOpen = true;
	}
	function close(): void {
		drawerOpen = false;
	}
	function finishClose(): void {
		if (!keepMounted) surfaceOpen = false;
		onClose();
	}
</script>

{#if callerControlled}
	<button type="button" data-drawer-trigger onclick={openDrawer}>Show drawer</button>
	<button type="button" data-drawer-teardown onclick={() => (surfaceOpen = false)}>Destroy drawer</button>
	{#if surfaceOpen}
		<!-- The caller surface remains through the AdaptiveDrawer outro. -->
		<AdaptiveDrawer ariaLabel="Focused action" open={drawerOpen} onClose={close} onClosed={finishClose} {fill} {followKeyboard} {modal} {detent} {contextLabel}>
			<button type="button" data-drawer-autofocus>Primary action</button>
			{#if tall}
				<div class="tall-content">Long content</div>
			{:else}
				<p>Short content</p>
			{/if}
		</AdaptiveDrawer>
	{/if}
{:else}
	<AdaptiveDrawer ariaLabel="Focused action" {onClose} {fill} {followKeyboard} {modal} {detent} {onDetent} {covered} {contextLabel} headless={!!head}>
		<!-- A headless sheet's own head row, marked as a handle, with a control in it. -->
		{#if head}<div class="own-head" data-drawer-handle><h2>Own head</h2><button type="button" data-head-control onclick={head}>Close</button></div>{/if}
		<button type="button" data-drawer-autofocus>Primary action</button>
		{#if draft}<textarea data-draft aria-label="Comment"></textarea>{/if}
		{#if tall}
			<div class="tall-content">Long content</div>
		{:else}
			<p>Short content</p>
		{/if}
	</AdaptiveDrawer>
{/if}

<style>
	.tall-content { height: 1400px; }
</style>
