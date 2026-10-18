<script lang="ts">
	import { onMount } from 'svelte';
	import type { HostProvider } from '$lib/stores/providersV7';
	import { fetchSources } from '$lib/conductorSources';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	interface Props { provider: HostProvider; isExternal: boolean; isHarness: boolean; }
	const { provider, isExternal, isHarness }: Props = $props();
	let showLocalEmpty = $state(false);

	onMount(async () => {
		if (provider.name !== 'omlx') return;
		const t = get(token);
		if (!t) return;
		try {
			const source = (await fetchSources(t)).find((candidate) => candidate.source === 'omlx');
			showLocalEmpty = !source?.settings_download && source?.eligible !== false;
		} catch { showLocalEmpty = true; }
	});
</script>

{#if provider.name !== 'omlx' || showLocalEmpty}
	<div class="phase-stub">
	{#if provider.name === 'omlx'}
		No local models on this Mac. Add one that fits.
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
{/if}
