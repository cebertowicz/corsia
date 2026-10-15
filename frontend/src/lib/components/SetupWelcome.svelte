<script lang="ts">
	import { Button, Toggle } from '$lib/components/ui';

	interface Props {
		localEligible: boolean | undefined;
		freeDiskGb: number | undefined;
		modelSize: string;
		modelSizeGb: number;
		downloadModelLabel: string;
		localWanted: boolean;
		onLocalWanted: (wanted: boolean) => void;
		onCheckAgain: () => void;
	}

	let {
		localEligible,
		freeDiskGb,
		modelSize,
		modelSizeGb,
		downloadModelLabel,
		localWanted,
		onLocalWanted,
		onCheckAgain,
	}: Props = $props();
</script>

<div class="welcome-copy">
	{#if localEligible === false}
		<p>This computer can’t run a local model. You can connect Claude Code, Codex, or an API key in the next step.</p>
	{:else if (freeDiskGb ?? Infinity) < modelSizeGb}
		<p>Needs {modelSize} free; {freeDiskGb} GB is free.</p>
		<Button variant="chip" onclick={onCheckAgain}>Free up space, then check again</Button>
	{:else}
		<div class="download-choice">
			<div class="switch">
				<Toggle checked={localWanted} onchange={onLocalWanted} ariaLabel={`Download ${downloadModelLabel} now`} />
				<span>Download {downloadModelLabel} now</span>
			</div>
			<span class="local-meta">{modelSize} · runs on this computer</span>
		</div>
	{/if}
</div>

<style>
	.welcome-copy {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
		max-width: calc(var(--space-8) * 9);
		color: var(--text-secondary);
	}
	.welcome-copy p {
		margin: 0;
	}
	.download-choice {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}
	.switch {
		display: flex;
		gap: var(--space-2);
		align-items: center;
		color: var(--text-primary);
	}
	.local-meta {
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
	}
</style>
