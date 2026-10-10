<script lang="ts">
	import { warmToast } from '$lib/stores/warmToast';
</script>

{#if $warmToast.visible}
	<div class="warm-toast" class:done={$warmToast.status === 'done'}>
		{#if $warmToast.status === 'done'}
			<span class="warm-icon">&#10003;</span>
		{:else}
			<span class="warm-spinner"></span>
		{/if}
		<span class="warm-msg">{$warmToast.message}</span>
		{#if $warmToast.status !== 'done' && $warmToast.elapsed > 0}
			<span class="warm-elapsed">{$warmToast.elapsed}s</span>
		{/if}
	</div>
{/if}

<style>
	.warm-toast {
		display: flex; align-items: center; justify-content: center; gap: 6px;
		padding: 4px 0; font-size: var(--text-footnote);
		color: var(--text-muted); opacity: 0.7;
		transition: opacity var(--dur-drawer);
	}
	.warm-toast.done { color: var(--green); opacity: 0.8; }
	.warm-spinner {
		width: 10px; height: 10px;
		border: 1.5px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
		border-top-color: var(--text-muted);
		border-radius: var(--radius-circle);
		animation: warm-spin 0.8s linear infinite; flex-shrink: 0;
	}
	.warm-toast.done .warm-spinner { display: none; }
	@keyframes warm-spin { to { transform: rotate(360deg); } }
	.warm-icon { font-size: var(--text-footnote); }
	.warm-msg { font-style: italic; }
	.warm-elapsed { font-family: monospace; font-size: var(--text-caption); opacity: 0.5; }
</style>
