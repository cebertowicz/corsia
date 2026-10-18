<script lang="ts">
	import type { HostProvider } from '$lib/stores/providersV7';
	interface Props { provider: HostProvider; isExternal: boolean; isHarness: boolean; }
	const { provider, isExternal, isHarness }: Props = $props();
</script>

<div class="phase-stub">
	{#if provider.name === 'omlx'}
		oMLX hasn't registered any models yet. Add a model to download one that fits this Mac.
	{:else if isExternal}
		This endpoint hasn't listed any models. Some providers never list them: add their model IDs in the provider settings. Otherwise, check its URL and key there.
	{:else if isHarness}
		{provider.label} hasn't reported any models. Check that its CLI is installed and signed in.
	{:else if provider.type === 'cloud'}
		No models exposed by this provider yet. Try <em>Refresh models</em> in the provider settings.
	{:else}
		No models discovered yet. Check the provider's logs.
	{/if}
</div>
