<script lang="ts">
	import { onMount } from 'svelte';
	import type { HostProvider } from '$lib/stores/providersV7';
	import { fetchSources } from '$lib/conductorSources';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import ProviderModelsPhase from './ProviderModelsPhase.svelte';
	interface Props { provider: HostProvider; isExternal: boolean; isHarness: boolean; }
	const { provider, isExternal, isHarness }: Props = $props();
	let showLocalEmpty = $state(false);
	const isCorsiaOmlx = $derived(provider.server?.corsia_omlx === true);
	// "That computer" only for a server on another computer; a person's own oMLX here is just "in oMLX".
	const localEmptyMessage = $derived(
		isCorsiaOmlx
			? 'No local models on this computer. Add one that fits.'
			: `No models on ${provider.server?.instance_name ?? provider.label}. Add them in oMLX${provider.server?.on_this_mac === true ? '' : ' on that computer'}.`,
	);

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
	<ProviderModelsPhase message={
		provider.name === 'omlx'
			? localEmptyMessage
			: isExternal
				? "This endpoint hasn't listed any models. Some providers never list them: add their model IDs in the provider settings. Otherwise, check its URL and key there."
				: isHarness
					? `${provider.label} hasn't reported any models. Check that its CLI is installed and signed in.`
					: provider.type === 'cloud'
						? 'No models exposed by this provider. Try Refresh models in the provider settings.'
						: "No models discovered. Check the provider's logs."
	} />
{/if}
