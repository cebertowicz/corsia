<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';

	let { onClose, tall = false, fill = false, followKeyboard = false, callerControlled = false }: { onClose: () => void; tall?: boolean; fill?: boolean; followKeyboard?: boolean; callerControlled?: boolean } = $props();
	let surfaceOpen = $state(false);
	let drawerOpen = $state(false);
	let narrow = $state(true);

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
		<!-- Mirrors caller-controlled drawers: its outer surface survives the local outro. -->
		{#if narrow}
			<AdaptiveDrawer ariaLabel="Focused action" open={drawerOpen} onClose={close} onClosed={finishClose} {fill} {followKeyboard}>
				<button type="button" data-drawer-autofocus>Primary action</button>
				{#if tall}
					<div class="tall-content">Long content</div>
				{:else}
					<p>Short content</p>
				{/if}
			</AdaptiveDrawer>
		{/if}
	{/if}
{:else}
	<AdaptiveDrawer ariaLabel="Focused action" {onClose} {fill} {followKeyboard}>
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
