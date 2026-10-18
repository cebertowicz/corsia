<script lang="ts">
	import type { ModelCapability, ModelState } from '$lib/stores/providerModels';
	import Menu from '$lib/components/ui/Menu.svelte';
	import { createRowMenu } from '$lib/components/ui/rowMenu.svelte';
	import ModelRowActions from './ModelRowActions.svelte';

	interface Props {
		name: string;
		state?: ModelState;
		meta?: string;
		caps?: ModelCapability[];
		toggleOn?: boolean;
		/** What the switch does, as [label when on, label when off]: Unload/Load for a local engine. */
		toggleLabels?: [string, string];
		progress?: number;
		/** Whether a loading row shows its progress puck; a download has nothing to switch yet. */
		puck?: boolean;
		/** Short line under the row, e.g. why the model is missing from the chat picker. */
		warning?: string | null;
		/** Muted model metadata that should sit below the primary row details. */
		note?: string | null;
		onToggle?: () => void;
		onInfo?: () => void;
		onCancel?: () => void;
		onRetry?: () => void;
		/** Settings-owned local downloads can be removed from this row's action menu. */
		onDeleteDownload?: () => void;
	}

	const {
		name,
		state = 'default',
		meta = '',
		caps = [],
		toggleOn = false,
		toggleLabels = ['Unload', 'Load'],
		progress = 0,
		puck = true,
		warning = null,
		note = null,
		onToggle,
		onInfo,
		onCancel,
		onRetry,
		onDeleteDownload,
	}: Props = $props();

	const showCancel = $derived(state === 'loading' || state === 'cache_warming' || state === 'queued');
	const showRetry = $derived(state === 'error');
	const showInfo = $derived(!showCancel && !showRetry);
	const menu = createRowMenu<string>(() => !!onDeleteDownload);
	const menuItems = $derived(onDeleteDownload ? [{ id: 'delete-download', label: 'Delete download' }] : []);

	function selectMenuItem(id: string) {
		menu.close();
		if (id === 'delete-download') onDeleteDownload?.();
	}
	const metaClass = $derived(
		state === 'loading' ? 'loading' :
		state === 'cache_warming' ? 'warming' :
		state === 'error' ? 'error' :
		'',
	);
	const rowClass = $derived(
		state === 'loaded' ? 'loaded' :
		state === 'loading' ? 'loading-row' :
		state === 'cache_warming' ? 'warming-row' :
		state === 'error' ? 'error-row' :
		'',
	);
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex: focused group exposes the row-menu keyboard handler for ContextMenu and Shift+F10. -->
<div class="mr {rowClass}" class:has-warn={!!warning || !!note} style="--p: {progress}%" role={onDeleteDownload ? 'group' : undefined} aria-label={onDeleteDownload ? `Actions for ${name}` : undefined} tabindex={onDeleteDownload ? 0 : undefined} {...menu.rowHandlers(name)}>
	<span class="mr-toggle-wrap">
		{#if (state === 'loading' || state === 'cache_warming') && !puck}
			<!-- Keeps the grid column so the name stays aligned with its neighbours. -->
		{:else if state === 'loading' || state === 'cache_warming'}
			<span class="puck" class:warming={state === 'cache_warming'}>
				<span class="fill" style="width: {progress}%"></span>
			</span>
		{:else if state === 'queued'}
			<span class="puck queued"></span>
		{:else if state === 'error'}
			<span class="err-bubble" aria-label="Error">
				<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round">
					<path d="M12 3 2 21h20L12 3Zm0 6v6m0 3v.01"/>
				</svg>
			</span>
		{:else}
			<button
				type="button"
				class="tg sm"
				class:on={toggleOn}
				aria-label={toggleOn ? toggleLabels[0] : toggleLabels[1]}
				onclick={(e) => { e.stopPropagation(); onToggle?.(); }}
			></button>
		{/if}
	</span>

	<span class="mname" title={name}>{name}</span>

	<span class="meta {metaClass}">{meta}</span>

	<span class="caps">
		{#each caps as cap (cap)}
			<span class="cap" data-cap={cap} title={cap}>
				{#if cap === 'vision'}
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
						<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/>
						<circle cx="12" cy="12" r="3" fill="currentColor"/>
					</svg>
				{:else if cap === 'reasoning'}
					<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
						<path d="M9.5 2A3.5 3.5 0 0 0 6 5.5v.7A2.5 2.5 0 0 0 4 8.5v.6a2.5 2.5 0 0 0 .5 1.5A2.5 2.5 0 0 0 4 12v.6a2.5 2.5 0 0 0 1.2 2A2.5 2.5 0 0 0 5 16.4 2.6 2.6 0 0 0 7.5 19h.6A3.5 3.5 0 0 0 11 22a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1h-1.5zM14 2a1 1 0 0 0-1 1v18a1 1 0 0 0 1 1 3.5 3.5 0 0 0 2.9-3h.6a2.6 2.6 0 0 0 2.5-2.6 2.5 2.5 0 0 0-.2-1.8A2.5 2.5 0 0 0 21 12.6V12a2.5 2.5 0 0 0-.5-1.4A2.5 2.5 0 0 0 21 9.1v-.6A2.5 2.5 0 0 0 19 6.2v-.7A3.5 3.5 0 0 0 15.5 2H14z"/>
					</svg>
				{:else if cap === 'tools'}
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
						<path d="M14 7a4 4 0 0 0 5 5l-3-3 1-1 3 3a4 4 0 0 0-5-5L4 22l-2-2L13 9l1-2z"/>
					</svg>
				{/if}
			</span>
		{/each}
	</span>

	<ModelRowActions {showInfo} {showCancel} {showRetry} {onInfo} {onCancel} {onRetry} />

	{#if warning}
		<span class="warn" role="status">{warning}</span>
	{:else if note}
		<span class="note">{note}</span>
	{/if}
</div>

{#if menu.open}
	<Menu
		ariaLabel={`Actions for ${name}`}
		items={menuItems}
		x={menu.open.x}
		y={menu.open.y}
		onSelect={selectMenuItem}
		onClose={menu.close}
	/>
{/if}

<style>
	.mr {
		display: grid;
		/* The name keeps a floor, so a long status line wraps instead of squeezing it to nothing. */
		grid-template-columns: 32px minmax(35%, 1fr) auto auto auto;
		grid-template-areas: "toggle name meta caps action";
		align-items: center;
		gap: 12px;
		padding: 10px 18px;
		transition: all var(--dur-tight) var(--ease-snap);
		position: relative;
	}
	.mr-toggle-wrap { grid-area: toggle; }
	.mname { grid-area: name; }
	.meta { grid-area: meta; }
	.caps { grid-area: caps; }

	.mr.has-warn {
		grid-template-areas:
			"toggle name meta caps action"
			".      warn warn warn warn";
		row-gap: 2px;
	}
	.warn {
		grid-area: warn;
		font-size: var(--text-footnote);
		color: var(--amber);
	}
	.mr:hover { background: var(--bg-hover); }
	.mr + :global(.mr) { box-shadow: inset 0 1px 0 var(--border-subtle); }

	.mr.loaded { background: var(--accent-wash); }
	.mr.loaded::before {
		content: '';
		position: absolute; left: 0; top: 4px; bottom: 4px;
		width: 2px; border-radius: 0 2px 2px 0;
		background: var(--accent);
	}
	.mr.loaded:hover { background: color-mix(in srgb, var(--accent) 16%, transparent); }

	.mr.loading-row { background: var(--amber-bg); }
	.mr.loading-row::before {
		content: '';
		position: absolute; left: 0; top: 4px; bottom: 4px;
		width: 2px; border-radius: 0 2px 2px 0;
		background: var(--amber);
	}
	.mr.loading-row::after {
		content: '';
		position: absolute; left: 14px; right: 14px; bottom: 0; height: 2px;
		background: linear-gradient(90deg, var(--amber) 0%, var(--amber) var(--p, 0%), transparent var(--p, 0%));
		border-radius: 1px;
	}

	.mr.warming-row { background: var(--sky-bg); }
	.mr.warming-row::before {
		content: '';
		position: absolute; left: 0; top: 4px; bottom: 4px;
		width: 2px; border-radius: 0 2px 2px 0;
		background: var(--sky);
	}
	.mr.warming-row::after {
		content: '';
		position: absolute; left: 14px; right: 14px; bottom: 0; height: 2px;
		background: linear-gradient(90deg, var(--sky) 0%, var(--sky) var(--p, 0%), transparent var(--p, 0%));
		border-radius: 1px;
	}

	.mr.error-row { background: var(--red-bg); }
	.mr.error-row::before {
		content: '';
		position: absolute; left: 0; top: 4px; bottom: 4px;
		width: 2px; border-radius: 0 2px 2px 0;
		background: var(--red);
	}

	.mr-toggle-wrap { display: flex; justify-content: center; }

	.mname {
		font-size: var(--text-subhead);
		font-weight: 500;
		color: var(--text-primary);
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
		min-width: 0;
		letter-spacing: -0.005em;
	}

	.meta {
		font-size: var(--text-footnote);
		color: var(--text-tertiary);
		display: flex; gap: 7px; align-items: center;
		font-variant-numeric: tabular-nums;
		flex-shrink: 0;
	}
	.meta.loading { color: var(--amber); font-weight: 500; }
	.meta.warming { color: var(--sky); font-weight: 500; }
	.meta.error   { color: var(--red); font-weight: 500; }
	.note { grid-area: warn; color: var(--text-tertiary); font-size: var(--text-footnote); }

	.caps { display: flex; gap: 4px; align-items: center; }

	.cap {
		display: inline-flex; align-items: center; justify-content: center;
		width: 22px; height: 22px;
		border-radius: var(--radius-xs);
		transition: all var(--dur-tight) var(--ease-snap);
		cursor: default;
	}
	.cap[data-cap="vision"]    { background: var(--sky-bg);    color: var(--sky);    box-shadow: inset 0 0 0 1px var(--sky-line); }
	.cap[data-cap="reasoning"] { background: var(--purple-bg); color: var(--purple-text); box-shadow: inset 0 0 0 1px var(--purple-line); }
	.cap[data-cap="tools"]     { background: var(--teal-bg);   color: var(--teal);   box-shadow: inset 0 0 0 1px var(--teal-line); }

	.tg {
		--w: 30px; --h: 17px;
		width: var(--w); height: var(--h);
		background: var(--bg-tertiary);
		border: 1px solid var(--border);
		border-radius: var(--radius-pill);
		position: relative;
		transition: all var(--dur-tight) var(--ease-snap);
		flex-shrink: 0;
		cursor: pointer;
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.3);
	}
	.tg::after {
		content: '';
		position: absolute;
		width: 12px; height: 12px;
		border-radius: var(--radius-circle);
		background: linear-gradient(180deg, #d8d8e0, #b8b8c0);
		top: 1.5px; left: 1.5px;
		transition: all var(--dur-tight) var(--ease-snap);
		box-shadow: 0 1px 2px rgba(0,0,0,0.4);
	}
	.tg.on {
		background: linear-gradient(180deg, var(--accent-hover), var(--accent));
		border-color: var(--accent);
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.2), 0 0 8px color-mix(in srgb, var(--accent) 30%, transparent);
	}
	.tg.on::after {
		left: calc(var(--w) - 14.5px);
		background: linear-gradient(180deg, #ffffff, #f0f0f5);
	}

	.puck {
		--w: 30px; --h: 17px;
		width: var(--w); height: var(--h);
		background: var(--amber-bg);
		border: 1px solid var(--amber-line);
		border-radius: var(--radius-pill);
		position: relative; overflow: hidden;
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.3);
	}
	.puck.warming {
		background: var(--sky-bg);
		border-color: var(--sky-line);
	}
	.puck::before {
		content: '';
		position: absolute; inset: 0;
		background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--amber) 40%, transparent), transparent);
		background-size: 200% 100%;
		animation: shimmer 1.4s linear infinite;
	}
	.puck.warming::before {
		background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--sky) 40%, transparent), transparent);
		background-size: 200% 100%;
	}
	.puck > .fill {
		position: absolute; left: 0; top: 0; bottom: 0;
		background: linear-gradient(180deg, var(--amber), #d97706);
		opacity: 0.5;
	}
	.puck.warming > .fill {
		background: linear-gradient(180deg, var(--sky), #2563eb);
	}
	.puck.queued {
		background: var(--bg-tertiary);
		border-color: var(--border);
		opacity: 0.55;
	}
	.puck.queued::before { display: none; }
	@keyframes shimmer {
		0%   { background-position: 200% 0; }
		100% { background-position: -200% 0; }
	}

	.err-bubble {
		display: inline-flex; align-items: center; justify-content: center;
		width: 30px; height: 17px;
		background: var(--red-bg);
		border: 1px solid color-mix(in srgb, var(--red) 28%, transparent);
		border-radius: var(--radius-pill);
		color: var(--red);
	}


	@media (max-width: 480px) {
		.mr {
			grid-template-columns: 28px 1fr auto;
			grid-template-areas:
				"toggle name action"
				".      meta action"
				".      caps action";
			gap: 4px 10px;
			padding: 9px 12px;
			align-items: start;
		}
		.mname { font-size: var(--text-subhead); line-height: 1.3; padding-top: 2px; }
		.meta  { font-size: var(--text-footnote); }
		.caps  { padding-top: 2px; }

		.mr.has-warn {
			grid-template-areas:
				"toggle name action"
				".      meta action"
				".      caps action"
				".      warn action";
		}
	}
</style>
