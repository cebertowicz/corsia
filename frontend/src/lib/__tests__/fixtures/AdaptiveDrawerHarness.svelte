<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';

	let { onClose, tall = false, fill = false, followKeyboard = false, callerControlled = false }: { onClose: () => void; tall?: boolean; fill?: boolean; followKeyboard?: boolean; callerControlled?: boolean } = $props();
	let surfaceOpen = $state(false);
	let narrow = $state(true);

	function close(): void {
		surfaceOpen = false;
		onClose();
	}
</script>

{#if callerControlled}
	<button type="button" data-drawer-trigger onclick={() => (surfaceOpen = true)}>Show drawer</button>
	{#if surfaceOpen}
		<!-- Mirrors DocCommentSurface: the caller owns an outer mounted surface. -->
		{#if narrow}
			<AdaptiveDrawer ariaLabel="Focused action" onClose={close} {fill} {followKeyboard}>
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
	<AdaptiveDrawer ariaLabel="Focused action" onClose={close} {fill} {followKeyboard}>
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
