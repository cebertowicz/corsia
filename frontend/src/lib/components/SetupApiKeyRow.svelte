<script lang="ts">
	import { Button, Field } from '$lib/components/ui';
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { loadProviders, updateProvider, providers } from '$lib/stores/providers';

	/** Opens ready to paste. `onSaved` runs once the key is stored; `onError` gets the message, or '' when a new try starts. */
	interface Props { providerName: string; onSaved: () => Promise<void>; onError: (message: string) => void }
	let { providerName = $bindable(), onSaved, onError }: Props = $props();
	let apiKey = $state('');
	let saving = $state(false);

	// Setup opens before anything has loaded the provider list, so the select would be empty until the key was saved.
	onMount(() => { if (!get(providers).length) void loadProviders(); });

	function focusOnMount(node: HTMLElement) { setTimeout(() => node.focus()); }

	async function save() {
		if (!apiKey.trim() || saving) return;
		saving = true; onError('');
		try {
			const result = await updateProvider(providerName, { api_key: apiKey });
			if (!result.ok) throw new Error(result.error);
			apiKey = ''; await onSaved();
		} catch { onError('CORSIA couldn’t save that API key. Check it and try again.'); }
		finally { saving = false; }
	}
</script>

<Field variant="chip" layout="stacked" label="API key">
	<select aria-label="Provider" bind:value={providerName}>{#each $providers.filter((provider) => provider.type === 'cloud') as provider}<option value={provider.name}>{provider.label}</option>{/each}</select>
	<input type="password" aria-label="API key" placeholder="Paste your key" bind:value={apiKey} use:focusOnMount />
	<Button variant="chip" size="sm" onclick={save} disabled={!apiKey.trim() || saving || !$providers.some((provider) => provider.name === providerName)}>{saving ? 'Saving…' : 'Save'}</Button>
</Field>
