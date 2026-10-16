<script lang="ts">
	import { Button, Toggle } from '$lib/components/ui';

	interface Props {
		localEligible?: boolean;
		freeDiskGb?: number;
		modelSizeGb?: number;
		modelSize: string;
		modelLabel: string;
		wanted: boolean;
		onWantedChange: (next: boolean) => void;
		onRecheck: () => void;
	}
	const { localEligible, freeDiskGb, modelSizeGb, modelSize, modelLabel, wanted, onWantedChange, onRecheck }: Props = $props();
</script>

<div class="welcome-copy">
	{#if localEligible === false}
		<p>This computer doesn’t have enough memory to run an AI model itself. CORSIA will use Claude Code or Codex with your subscription, or an API key.</p>
	{:else if (freeDiskGb ?? Infinity) < (modelSizeGb ?? 0)}
		<p>Needs {modelSize} free; {freeDiskGb} GB is free.</p>
		<Button variant="chip" onclick={onRecheck}>Free up space, then check again</Button>
	{:else}
		<div class="download-choice">
			<span class="switch"><Toggle checked={wanted} onchange={onWantedChange} ariaLabel="Also run AI on this computer" /></span>
			<div class="choice-copy">
				<span class="choice-name">Also run AI on this computer</span>
				<span class="why">Free, private, and works offline. It’s slower than Claude or ChatGPT, so it’s for work you’d rather keep on this computer, and you can move background jobs onto it.</span>
				<span class="spec">{modelLabel} · {modelSize} · runs in oMLX</span>
				<span class="spec">The download starts in the background when you press Get started.</span>
			</div>
		</div>
	{/if}
</div>

<style>
	.welcome-copy { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	.welcome-copy p { margin: 0; }
	.download-choice { display: flex; align-items: flex-start; gap: var(--space-3); }
	.switch { flex: none; display: inline-flex; padding-top: var(--space-1); }
	.choice-copy { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
	.choice-name { color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-medium); }
	.why { color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); text-wrap: pretty; }
	.spec { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: var(--lh-normal); text-wrap: pretty; }
</style>
