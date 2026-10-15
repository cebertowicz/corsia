<script lang="ts">
	import { Button, Field, ListRow, Message, StatePill } from '$lib/components/ui';
	import { sourceIsReady, type ModelSource } from '$lib/conductorSources';
	import type { ProviderEntry } from '$lib/stores/providers';

	interface Props {
		sources: ModelSource[];
		modelSize: string;
		localDownloading: boolean;
		localRepairDeferred: boolean;
		localStartError: string;
		localSetupBusy: boolean;
		sourceError: string;
		keyOpen: boolean;
		providerName: string;
		apiKey: string;
		savingKey: boolean;
		providers: ProviderEntry[];
		onRetryLocalDownload: () => void;
		onRefreshSources: () => void;
		onSignIn: (source: ModelSource) => void;
		onOpenKey: () => void;
		onProviderName: (name: string) => void;
		onApiKey: (key: string) => void;
		onSaveApiKey: () => void;
	}

	let {
		sources,
		modelSize,
		localDownloading,
		localRepairDeferred,
		localStartError,
		localSetupBusy,
		sourceError,
		keyOpen,
		providerName,
		apiKey,
		savingKey,
		providers,
		onRetryLocalDownload,
		onRefreshSources,
		onSignIn,
		onOpenKey,
		onProviderName,
		onApiKey,
		onSaveApiKey,
	}: Props = $props();

	function sourceMeta(source: ModelSource): string {
		if (source.source === 'omlx') {
			if (source.setup_downloading || source.setup_stage) return source.setup_stage || 'Downloading';
			if (localRepairDeferred) return "oMLX doesn't start";
			if (source.setup_error || localStartError) return 'Download needs attention';
			return `Local model · ${modelSize}`;
		}
		if (source.state === 'not_installed') return `${source.name} needs its official app installed first.`;
		if (source.state === 'needs_auth') return 'Sign in to use your subscription';
		if (sourceIsReady(source)) return 'Ready to use.';
		return 'Check this source, then try again.';
	}
</script>

<div class="sources">
	{#each sources as source (source.source)}
		<ListRow name={source.source === 'omlx' ? 'This computer' : source.name} meta={[sourceMeta(source)]} centered>
			{#snippet actions()}
				{#if sourceIsReady(source)}
					<StatePill look="dot" state="success" label="Ready" />
				{:else if source.source === 'omlx' && localDownloading}
					<StatePill look="dot" state="loading" label="Downloading" />
				{:else if source.source === 'omlx'}
					<Button variant="chip" size="sm" loading={localSetupBusy} disabled={localSetupBusy} onclick={onRetryLocalDownload}>
						{localRepairDeferred ? 'Reinstall' : source.setup_error || localStartError ? 'Try again' : 'Download'}
					</Button>
				{:else if source.state === 'not_installed'}
					<StatePill look="dot" state="idle" label="Not installed" />
					<Button variant="chip" size="sm" onclick={onRefreshSources}>Check again</Button>
				{:else}
					<Button variant="chip" size="sm" onclick={() => onSignIn(source)}>Sign in</Button>
				{/if}
			{/snippet}
		</ListRow>
	{/each}
	{#if keyOpen}
		<Field variant="chip" layout="stacked" label="API key">
			<select
				aria-label="Provider"
				value={providerName}
				onchange={(event) => onProviderName((event.currentTarget as HTMLSelectElement).value)}
			>
				{#each providers.filter((provider) => provider.type === 'cloud') as provider}
					<option value={provider.name}>{provider.label}</option>
				{/each}
			</select>
			<input
				type="password"
				aria-label="API key"
				placeholder="Paste your key"
				value={apiKey}
				oninput={(event) => onApiKey((event.currentTarget as HTMLInputElement).value)}
			/>
			<Button
				variant="chip"
				size="sm"
				onclick={onSaveApiKey}
				disabled={!apiKey.trim() || savingKey}
			>
				{savingKey ? 'Saving…' : 'Save'}
			</Button>
		</Field>
	{:else}
		<div class="setup-accent-text-action">
			<Button variant="chip" size="sm" onclick={onOpenKey}>Use an API key instead</Button>
		</div>
	{/if}
	{#if sourceError}<Message variant="err">{sourceError}</Message>{/if}
</div>

<style>
	.sources {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
		max-width: calc(var(--space-8) * 9);
		color: var(--text-secondary);
	}
</style>
