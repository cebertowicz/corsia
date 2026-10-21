<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';

	let { onClose, tall = false, fill = false, followKeyboard = false, modal = true, detent = 'full', callerControlled = false, covered = false, contextLabel = null }: { onClose: () => void; tall?: boolean; fill?: boolean; followKeyboard?: boolean; modal?: boolean; detent?: 'full' | 'half'; callerControlled?: boolean; covered?: boolean; contextLabel?: string | null } = $props();
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
		surfaceOpen = false;
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
	<AdaptiveDrawer ariaLabel="Focused action" {onClose} {fill} {followKeyboard} {modal} {detent} {covered} {contextLabel}>
		<button type="button" data-drawer-autofocus>Primary action</button>
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
