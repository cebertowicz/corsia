<script lang="ts">
	import { Button, Message, Modal } from '$lib/components/ui';

	interface Props {
		modelName: string;
		estimatedGb: number;
		freeGb: number;
		afterLoadGb: number;
		safeReserveGb: number;
		warning: string;
		onConfirm: () => void;
		onCancel: () => void;
	}

	const {
		modelName, estimatedGb, freeGb, afterLoadGb, safeReserveGb, warning,
		onConfirm, onCancel,
	}: Props = $props();

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') onConfirm();
	}

	const overflows = $derived(afterLoadGb < safeReserveGb);
	// Past zero the load needs more memory than is free, so "tight" would understate it.
	const exceeds = $derived(afterLoadGb < 0);
	const usedPct = $derived(freeGb > 0 ? Math.min(100, Math.max(0, ((freeGb - estimatedGb) / freeGb) * 100)) : 100);
</script>

<svelte:window onkeydown={onKeydown} />

<Modal
	title={exceeds ? 'Not enough memory' : 'Tight on memory'}
	size="sm"
	role="alertdialog"
	onClose={onCancel}
>
	<p class="lede">
		Loading <strong>{modelName}</strong> ({estimatedGb.toFixed(0)} GB) would
		{exceeds ? 'need more memory than is free.' : 'push free memory close to the limit.'}
	</p>

	<div class="bar">
		<div class="bar-track">
			<i class="b-used" style="width: {usedPct}%"></i>
			<i class="b-overflow" class:visible={overflows}></i>
		</div>
		<div class="bar-stats">
			<span><strong>{freeGb.toFixed(0)} GB</strong> free now</span>
			<span class="sep">·</span>
			<span class:warn={overflows}>
				<strong>{afterLoadGb.toFixed(0)} GB</strong> free after load
			</span>
			<span class="sep">·</span>
			<span class="reserve"><strong>{safeReserveGb.toFixed(0)} GB</strong> safe reserve</span>
		</div>
	</div>

	<Message variant="err">{warning}</Message>

	<p class="hint">
		macOS may swap or kill processes if memory runs out. Unload other
		models to free memory, or load anyway.
	</p>

	{#snippet foot()}
		<Button variant="ghost" onclick={onCancel}>Cancel</Button>
		<Button variant="danger" onclick={onConfirm}>Load anyway</Button>
	{/snippet}
</Modal>

<style>
	.lede {
		font-size: var(--text-subhead); color: var(--text-primary);
		margin: 0 0 14px; line-height: 1.5;
	}
	.lede strong { color: var(--accent-text, var(--accent)); }

	.bar { margin: 14px 0 16px; }
	.bar-track {
		position: relative;
		height: 10px;
		background: var(--bg-tertiary);
		border-radius: var(--radius-2xs);
		overflow: hidden;
		margin-bottom: 6px;
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.4);
	}
	.bar-track > .b-used {
		display: block; height: 100%;
		background: linear-gradient(180deg, var(--amber), #d97706);
		transition: width var(--dur-standard) var(--ease-snap);
	}
	.bar-track > .b-overflow.visible {
		position: absolute; right: 0; top: 0; bottom: 0; width: 4px;
		background: var(--red);
		opacity: 0.7;
		animation: blink 1s infinite;
	}
	@keyframes blink {
		0%, 100% { opacity: 0.4; }
		50% { opacity: 1; }
	}
	.bar-stats {
		display: flex; flex-wrap: wrap; gap: 8px;
		font-size: var(--text-footnote); color: var(--text-tertiary);
		font-variant-numeric: tabular-nums;
	}
	.bar-stats strong { color: var(--text-primary); font-weight: 600; }
	.bar-stats .sep { color: var(--text-tertiary); opacity: 0.5; }
	.bar-stats .warn strong { color: var(--amber); }
	.bar-stats .reserve strong { color: var(--green); }

	.hint {
		font-size: var(--text-footnote); color: var(--text-tertiary);
		line-height: 1.6; margin: 0;
	}
</style>
