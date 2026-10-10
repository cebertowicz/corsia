<script lang="ts">
	import type { UsageSegment } from '$lib/stores/modelGroups';

	interface Props {
		name: string;
		groupKey: string;
		count: number;
		local?: boolean;
		fallback?: boolean;
		open?: boolean;
		status?: UsageSegment[];
		subtitle?: UsageSegment[];
		breakdown?: string[];
		usageOpen?: boolean;
		onToggle?: () => void;
		onToggleUsage?: () => void;
	}

	let {
		name,
		groupKey,
		count,
		local = false,
		fallback = false,
		open = false,
		status = [],
		subtitle = [],
		breakdown = [],
		usageOpen = false,
		onToggle,
		onToggleUsage,
	}: Props = $props();
	const statusLabel = $derived(status.every((segment) => segment.label) ? status.map((segment) => segment.label).join(', ') : undefined);
	const subtitleLabel = $derived(subtitle.every((segment) => segment.label) ? subtitle.map((segment) => segment.label).join(', ') : undefined);
</script>

<div class="model-group-bar" data-variant={fallback ? 'fallback' : local ? 'local' : 'group'}>
	{#if local || fallback}
		<span class="model-group-head"><span class="group-name">{name}</span>{#if local}<span class="local-status" aria-hidden="true">{' · Local · ready'}</span>{/if}</span>
	{:else}
		<button
			type="button"
			class="model-group-head press-tint"
			aria-expanded={open}
			aria-controls={`model-group-${groupKey}`}
			onclick={onToggle}
		>
			<svg class="group-chevron" class:open width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
			<span class="group-name">{name}</span>
			<span class="group-count" aria-label={`${count} ${count === 1 ? 'model' : 'models'}`}>{count}</span>
			{#if status.length > 0}
				<span class="group-status" role="group" aria-label={statusLabel}>
					{#each status as segment, index}
						{#if index > 0}<span aria-hidden="true">{' · '}</span>{/if}<span aria-hidden={statusLabel ? 'true' : undefined} class:usage-amber={segment.level === 'amber'} class:usage-red={segment.level === 'red'}>{segment.text}</span>
					{/each}
				</span>
			{/if}
		</button>
	{/if}
</div>
{#if subtitle.length > 0}
	{#if breakdown.length > 0}
		<button
			type="button"
			class="group-subtitle press-tint"
			aria-label={subtitleLabel}
			aria-expanded={usageOpen}
			aria-controls={`model-usage-${groupKey}`}
			onclick={onToggleUsage}
		>
			{#each subtitle as segment, index}
				{#if index > 0}<span aria-hidden="true">{' · '}</span>{/if}<span aria-hidden={subtitleLabel ? 'true' : undefined} class:usage-amber={segment.level === 'amber'} class:usage-red={segment.level === 'red'}>{segment.text}</span>
			{/each}
		</button>
	{:else}
		<div class="group-subtitle" role="group" aria-label={subtitleLabel}>
			{#each subtitle as segment, index}
				{#if index > 0}<span aria-hidden="true">{' · '}</span>{/if}<span aria-hidden={subtitleLabel ? 'true' : undefined} class:usage-amber={segment.level === 'amber'} class:usage-red={segment.level === 'red'}>{segment.text}</span>
			{/each}
		</div>
	{/if}
{/if}
{#if usageOpen && breakdown.length > 0}
	<div class="group-usage" id={`model-usage-${groupKey}`}>
		{#each breakdown as line}<div>{line}</div>{/each}
	</div>
{/if}

<style>
	.model-group-bar { display: flex; align-items: flex-start; }
	.model-group-head { flex: 1; min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; padding: 8px 10px; font-family: inherit; font-size: var(--text-subhead); color: var(--text-primary); background: none; border: none; border-radius: var(--radius-xs); text-align: left; }
	button.model-group-head { cursor: pointer; }
	button.model-group-head:hover { background: var(--bg-hover); }
	.group-chevron { flex-shrink: 0; color: var(--text-tertiary); transition: transform var(--dur-tight); }
	.group-chevron.open { transform: rotate(90deg); }
	.group-name { font-weight: 600; }
	.group-count, .local-status, .group-status { font-size: var(--text-caption); color: var(--text-tertiary); }
	.group-status { margin-left: auto; text-align: right; }
	.group-status .usage-amber { color: var(--amber-text); }
	.group-status .usage-red { color: var(--red-text); }
	.group-subtitle { display: block; width: 100%; box-sizing: border-box; padding: 6px 10px 8px 32px; font-family: inherit; font-size: var(--text-caption); color: var(--text-tertiary); background: none; border: none; border-radius: var(--radius-xs); text-align: left; }
	button.group-subtitle { cursor: pointer; }
	button.group-subtitle:hover { background: var(--bg-hover); }
	.group-subtitle .usage-amber { color: var(--amber-text); }
	.group-subtitle .usage-red { color: var(--red-text); }
	.group-usage { padding: 2px 10px 6px 32px; font-size: var(--text-caption); color: var(--text-tertiary); }
</style>
