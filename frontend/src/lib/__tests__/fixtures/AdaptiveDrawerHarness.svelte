<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';

	let { onClose, tall = false, fill = false, followKeyboard = false }: { onClose: () => void; tall?: boolean; fill?: boolean; followKeyboard?: boolean } = $props();
</script>

<AdaptiveDrawer ariaLabel="Focused action" {onClose} {fill} {followKeyboard}>
	<button type="button" data-drawer-autofocus>Primary action</button>
	{#if tall}
		<div class="tall-content">Long content</div>
	{:else}
		<p>Short content</p>
	{/if}
</AdaptiveDrawer>

<style>
	.tall-content { height: 1400px; }
</style>
