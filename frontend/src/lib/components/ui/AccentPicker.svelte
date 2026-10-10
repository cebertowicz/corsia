<script lang="ts">
	import { ACCENT_SWATCHES, ACCENT_AUTO_GRADIENT } from '$lib/utils/projectColors';

	let { value = null, onPick, onPreview }: {
		value?: string | null;
		onPick: (color: string | null) => void;
		onPreview?: (color: string) => void;
	} = $props();

	const isCustom = $derived(value !== null && !ACCENT_SWATCHES.includes(value));
	// svelte-ignore state_referenced_locally
	const initialSeed =
		value !== null && !ACCENT_SWATCHES.includes(value) ? value : '#8b5cf6';
</script>

<div class="picker">
	<button
		type="button"
		class="chip"
		class:on={value === null}
		onclick={() => onPick(null)}
		title="Auto — a distinct color per project"
		aria-label="Auto accent"
	>Auto</button>

	<div class="grid" role="group" aria-label="Accent colors">
		{#each ACCENT_SWATCHES as c}
			<button
				type="button"
				class="sw"
				class:on={value === c}
				style="background:{c}"
				onclick={() => onPick(c)}
				aria-label="Accent {c}"
			></button>
		{/each}

		<label class="sw custom" class:on={isCustom} style="background-image:{ACCENT_AUTO_GRADIENT}" title="Custom color…">
			{#if isCustom && value}
				<span class="custom-dot" style="background:{value}"></span>
				<span class="sr-only">Custom accent selected: {value}</span>
			{/if}
			<input
				type="color"
				value={initialSeed}
				aria-label="Custom accent color"
				oninput={(e) => onPreview?.((e.target as HTMLInputElement).value)}
				onchange={(e) => onPick((e.target as HTMLInputElement).value)}
			/>
		</label>
	</div>
</div>

<style>
	.picker { display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }

	.chip {
		font: inherit; font-size: var(--text-footnote); font-weight: 600;
		padding: 4px 12px; border-radius: var(--radius-pill); cursor: pointer;
		background: var(--bg-active); color: var(--text-secondary);
		border: 2px solid transparent;
	}
	.chip:hover { color: var(--text-primary); }
	.chip.on { border-color: var(--text-primary); color: var(--text-primary); }

	.grid { display: grid; grid-template-columns: repeat(9, 26px); gap: 8px; }
	@media (max-width: 480px) { .grid { grid-template-columns: repeat(6, 26px); } }

	.sw {
		width: 26px; height: 26px; border-radius: var(--radius-circle);
		border: 2px solid transparent; cursor: pointer; padding: 0;
		position: relative;
	}
	.sw.on { border-color: var(--text-primary); box-shadow: 0 0 0 2px var(--bg-secondary) inset; }

	.sw.custom { background-clip: padding-box; display: block; }
	.custom-dot {
		position: absolute; inset: 5px; border-radius: var(--radius-circle);
		box-shadow: 0 0 0 1.5px var(--bg-secondary);
	}
	.sr-only {
		position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
		overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
	}
	.sw.custom input[type='color'] {
		position: absolute; inset: -2px; width: calc(100% + 4px); height: calc(100% + 4px);
		opacity: 0; cursor: pointer; border: none; padding: 0;
	}
</style>
