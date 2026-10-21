<script lang="ts">
	import { onMount } from 'svelte';
	import InputBarActionRow from '$lib/components/InputBarActionRow.svelte';
	import '$lib/components/InputBar.css';
	import { connectionState } from '$lib/stores/connection';

	const params = new URLSearchParams(window.location.search);
	const baseVariant = params.get('variant') === 'base';
	const estimated = params.get('source') === 'estimated';
	const contextPct = Number(params.get('pct') ?? 7);
	const theme = params.get('theme');
	const modelLabel = params.get('model') === 'long' ? 'Claude Opus 5.5 Extended' : 'GPT-6.1 Sol';
	let activeSheet = $state<'context' | null>(null);

	onMount(() => {
		connectionState.set('connected');
		if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
	});
</script>

<svelte:head><title>Context chip probe</title></svelte:head>

<main class="probe" class:base-variant={baseVariant}>
	<div class="input-floater">
		<div class="input-chip">
			<InputBarActionRow
				threadId="context-chip-probe"
				sheetView={activeSheet}
				openSheet={() => { activeSheet = activeSheet === 'context' ? null : 'context'; }}
				isActiveModelReady={true}
				canSendToModel={true}
				queueUntilSwitch={false}
				{modelLabel}
				activeModelName={modelLabel}
				supportsReasoning={true}
				effortLabel="High"
				canInspectContext={true}
				contextThreadName="Probe thread"
				contextUtilization={contextPct / 100}
				{contextPct}
				contextTokenSource={estimated ? 'estimated' : 'measured'}
				contextCountsAgainstBudget={false}
				inputText=""
				pendingAttachmentCount={0}
				hasMessageToSend={false}
				onSendClick={() => {}}
				onQueueAfterTurn={() => {}}
			/>
		</div>
	</div>
</main>

<style>
	:global(body) { padding: 0; background: var(--bg-primary); }
	.probe { position: relative; width: 100%; height: 120px; }
	:global(.base-variant .ctx-estimate-marker) { display: none; }
</style>
