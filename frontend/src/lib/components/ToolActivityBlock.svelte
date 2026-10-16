<script lang="ts">
	import {
		toolDetailResult,
		toolDisplayArgs,
		toolKind,
		toolOutcomeLabel,
		type ToolOutcome,
		summarizeTools,
		diffPayload,
		type ToolKind,
	} from '$lib/toolMeta';
	import { lineDiff, diffStats } from '$lib/diff';
	import ToolDiff from './ToolDiff.svelte';

	export interface ActivityTool {
		id: string;
		name: string;
		hint?: string;
		args?: string;
		result?: string;
		phrase?: string;
		object?: string;
		outcome?: ToolOutcome | null;
		errorLine?: string | null;
		resultState?: 'absent' | 'deferred' | 'loading' | 'failed' | 'loaded';
		resultError?: string;
		callId?: string;
		status?: 'running' | 'done';
	}

	let {
		tools,
		blockKey,
		live = false,
		expandedTools,
		onToggle,
		onLoadResult,
	}: {
		tools: ActivityTool[];
		blockKey: string;
		live?: boolean;
		expandedTools: Set<string>;
		onToggle: (id: string) => void;
		onLoadResult?: (tool: ActivityTool) => void;
	} = $props();

	const segments = $derived(summarizeTools(tools));
	const anyRunning = $derived(live && tools.some((t) => t.status === 'running'));
	const outcomeCounts = $derived(
		tools.reduce(
			(counts, tool) => {
				if (tool.outcome === 'failed') counts.failed += 1;
				if (tool.outcome === 'refused') counts.refused += 1;
				if (tool.outcome === 'unknown') counts.unknown += 1;
				return counts;
			},
			{ failed: 0, refused: 0, unknown: 0 }
		)
	);
	const outcomeSegments = $derived([
		...(outcomeCounts.failed ? [{ kind: 'failed', label: `${outcomeCounts.failed} failed` }] : []),
		...(outcomeCounts.refused ? [{ kind: 'refused', label: `${outcomeCounts.refused} refused` }] : []),
		...(outcomeCounts.unknown ? [{ kind: 'unknown', label: `${outcomeCounts.unknown} unknown` }] : []),
	]);
	const summarySegments = $derived([...segments, ...outcomeSegments]);
	const hasFailure = $derived(outcomeCounts.failed > 0 || outcomeCounts.refused > 0);
	// The check claims success, so it needs a 'done' outcome on every call. An interrupted call
	// (still 'running' after the turn ended) or one with no stored result gets no check and no count.
	const allDone = $derived(tools.length > 0 && tools.every((t) => t.outcome === 'done'));
	const summaryLabel = $derived(summarySegments.map((segment) => segment.label).join(' · '));
	const isExpanded = $derived(expandedTools.has(blockKey));

	const visibleRows = $derived(
		isExpanded ? tools : anyRunning ? tools.filter((t) => t.status === 'running') : []
	);

	function rowDiff(t: ActivityTool) {
		const payload = diffPayload(t.name, t.args);
		if (!payload) return null;
		const lines = lineDiff(payload.oldText, payload.newText);
		return lines ? { file: payload.file, lines } : null;
	}

	function cmdOf(args: unknown): string | null {
		if (args && typeof args === 'object') {
			const command = (args as Record<string, unknown>).command;
			return typeof command === 'string' ? command : null;
		}
		if (typeof args !== 'string' || !args) return null;
		try {
			const a = JSON.parse(args);
			return typeof a?.command === 'string' ? a.command : null;
		} catch {
			return null;
		}
	}

	function resultLines(result: string | undefined): string[] {
		if (!result) return [];
		return result.replace(/\n$/, '').split('\n').slice(0, 400);
	}

	function detailPayload(value: unknown): string {
		if (typeof value === 'string') return value;
		return JSON.stringify(value, null, 2);
	}

	function toggleToolRow(tool: ActivityTool): void {
		const opening = !expandedTools.has(tool.id);
		onToggle(tool.id);
		if (opening && tool.resultState === 'deferred') onLoadResult?.(tool);
	}

	const GLYPH: Record<ToolKind, string> = {
		read: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6',
		write: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6M12 18v-6M9 15h6',
		edit: 'M17 3a2.85 2.83 0 114 4L7.5 20.5 2 22l1.5-5.5z',
		exec: 'M4 17l6-6-6-6M12 19h8',
		search: 'M11 19a8 8 0 100-16 8 8 0 000 16zM21 21l-4.35-4.35',
		web: 'M12 22a10 10 0 100-20 10 10 0 000 20zM2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z',
		agent: 'M12 3l1.8 5 5 1.8-5 1.8L12 17l-1.8-5-5-1.8 5-1.8z',
		todo: 'M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11',
		memory: 'M9 4a3 3 0 00-3 3 3 3 0 00-3 3 3 3 0 001 5 3 3 0 005 1 3 3 0 003-3V4zM15 4a3 3 0 013 3 3 3 0 013 3 3 3 0 01-1 5 3 3 0 01-5 1 3 3 0 01-3-3V4z',
		generic: 'M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z',
	};
</script>

<div class="ta-block" class:running={anyRunning} class:open={visibleRows.length > 0}>
	<button class="ta-summary press-tint" aria-label={summaryLabel} onclick={() => onToggle(blockKey)}>
		{#if anyRunning}
			<svg class="ta-spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		{:else if hasFailure}
			<svg class="ta-failure" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>
		{:else if allDone}
			<svg class="ta-check" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
		{:else}
			<span class="ta-status-spacer" aria-hidden="true"></span>
		{/if}
		<span class="ta-segments">
			{#each summarySegments as seg, i (seg.kind)}
				{#if i > 0}<span class="ta-sep">·</span>{/if}
				<span class="ta-seg" class:bad={seg.kind === 'failed' || seg.kind === 'refused'} class:unknown={seg.kind === 'unknown'}>{seg.label}</span>
			{/each}
		</span>
		<svg class="ta-chevron" class:rotated={isExpanded} width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
	</button>

	{#if visibleRows.length > 0}
		<div class="ta-rows">
			{#each visibleRows as t (t.id)}
				{@const displayArgs = toolDisplayArgs(t.name, t.args)}
				{@const displayResult = toolDetailResult(t.name, t.result)}
				{@const kind = toolKind(t.name, t.args)}
				{@const isRunning = t.status === 'running'}
				{@const rowExpanded = expandedTools.has(t.id)}
				{@const diff = rowExpanded ? rowDiff(t) : null}
				{@const badge = !rowExpanded ? rowDiff(t) : diff}
				<div class="ta-row-wrap">
					<button class="ta-row press-tint" class:active={isRunning} disabled={isRunning} onclick={() => toggleToolRow(t)}>
						{#if isRunning}
							<svg class="ta-spin row-glyph" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
						{:else}
							<svg class="row-glyph" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d={GLYPH[kind]}/></svg>
						{/if}
						<span class="ta-phrase" title={t.phrase}>{t.phrase}</span>
						{#if !isRunning && t.outcome}
							<span class="ta-outcome" class:bad={t.outcome === 'failed' || t.outcome === 'refused'}>{toolOutcomeLabel(t.outcome, t.errorLine)}</span>
						{/if}
						{#if badge}
							{@const s = diffStats(badge.lines)}
							<span class="ta-badge"><i class="add">+{s[0]}</i><i class="del">−{s[1]}</i></span>
						{/if}
						{#if !isRunning}
							<svg class="ta-chevron" class:rotated={rowExpanded} width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
						{/if}
					</button>
					{#if rowExpanded}
						<div class="ta-detail">
							{#if t.resultState === 'loading'}
								<div class="ta-load-state"><span class="ta-spin" aria-hidden="true"></span> Loading output…</div>
							{:else if t.resultState === 'failed'}
								<div class="ta-load-error">{t.resultError ?? 'Could not load output.'} <button type="button" class="ta-retry" onclick={() => onLoadResult?.(t)}>Retry</button></div>
							{:else if t.resultState === 'absent'}
								<div class="ta-load-state">No output.</div>
							{:else if t.resultState === 'deferred' && !onLoadResult}
								<div class="ta-load-state">Output unavailable in this view.</div>
							{:else if t.resultError}
									<div class="ta-load-error">{t.resultError} <button type="button" class="ta-retry" onclick={() => onLoadResult?.(t)}>Retry</button></div>
								{:else if diff}
								<ToolDiff file={diff.file} lines={diff.lines} />
							{:else if kind === 'exec' && cmdOf(displayArgs)}
								<pre class="ta-cmd"><span class="ta-cmd-mark">$</span> {cmdOf(displayArgs)}</pre>
								{#if displayResult}
									<div class="ta-well"><pre class="ta-well-pre">{displayResult}</pre></div>
								{/if}
							{:else if kind === 'read' && displayResult}
								<div class="ta-well read">
									{#each resultLines(displayResult) as line, i (i)}
										<div class="ta-rl"><span class="ta-ln">{i + 1}</span><span class="ta-lc">{line || ' '}</span></div>
									{/each}
								</div>
							{:else}
								{#if displayArgs}
									<div class="ta-section">
										<div class="ta-label">Input</div>
										<pre class="ta-code">{detailPayload(displayArgs)}</pre>
									</div>
								{/if}
								{#if displayResult}
									<div class="ta-section">
										<div class="ta-label">Output</div>
										<pre class="ta-code">{displayResult}</pre>
									</div>
								{/if}
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.ta-block {
		width: fit-content; max-width: 100%;
		border-radius: var(--radius);
		border: 1px solid transparent;
		overflow: hidden;
		transition: background var(--dur-standard) ease, border-color var(--dur-standard) ease;
	}
	.ta-block.open {
		background: color-mix(in srgb, var(--bg-secondary) 60%, transparent);
		border-color: var(--border-subtle);
	}
	.ta-block.running.open {
		border-color: color-mix(in srgb, var(--accent) 35%, var(--border-subtle));
	}

	.ta-summary {
		display: flex; align-items: center; gap: 7px;
		width: 100%;
		padding: 5px 10px;
		background: none; border: none; cursor: pointer;
		font-family: inherit; font-size: var(--text-footnote);
		color: var(--text-tertiary);
		text-align: left;
		border-radius: var(--radius);
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.ta-summary:hover { background: var(--bg-hover); color: var(--text-secondary); }
	.ta-check { flex-shrink: 0; color: var(--green); opacity: 0.8; }
	.ta-failure { flex-shrink: 0; color: var(--red); }
	.ta-status-spacer { flex: 0 0 12px; }
	.ta-spin { flex-shrink: 0; color: var(--status-in-progress); animation: ta-spin 1s linear infinite; }
	.ta-segments {
		flex: 1; min-width: 0;
		display: flex; align-items: center; flex-wrap: wrap;
		column-gap: 5px; row-gap: 2px;
	}
	.ta-seg { white-space: nowrap; }
	.ta-seg.bad { color: var(--red); }
	.ta-seg.unknown { color: var(--text-tertiary); }
	.ta-sep { color: var(--text-tertiary); opacity: 0.5; }
	.ta-chevron { flex-shrink: 0; color: var(--text-tertiary); opacity: 0.6; transition: transform var(--dur-tight) ease; }
	.ta-chevron.rotated { transform: rotate(180deg); }

	.ta-rows {
		position: relative;
		border-top: 1px solid var(--border-subtle);
		padding: 4px 4px 4px 0;
		display: flex; flex-direction: column;
		animation: ta-in var(--dur-tight) ease-out;
	}
	.ta-rows::before {
		content: ''; position: absolute;
		left: 17px; top: 8px; bottom: 8px; width: 1px;
		background: var(--border-subtle);
	}
	.ta-row-wrap { display: flex; flex-direction: column; }
	.ta-row {
		display: flex; align-items: center; gap: 8px;
		width: 100%;
		padding: 5px 8px 5px 6px;
		background: none; border: none; cursor: pointer;
		border-radius: var(--radius-sm);
		font-family: inherit; font-size: var(--text-footnote);
		color: var(--text-secondary);
		text-align: left;
		transition: background var(--dur-tight);
	}
	.ta-row:hover { background: var(--bg-hover); }
	.ta-row.active { color: var(--accent-scope-text); cursor: default; }
	.row-glyph {
		flex-shrink: 0; color: var(--text-tertiary);
		background: var(--bg-secondary); border-radius: var(--radius-circle);
		padding: 2px; box-sizing: content-box; z-index: 1;
	}
	.ta-row.active .row-glyph { color: var(--accent-scope-text); }
	.ta-phrase {
		flex: 1; min-width: 0; color: var(--text-primary);
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.ta-row.active .ta-phrase { color: var(--accent-scope-text); }
	.ta-outcome {
		flex-shrink: 0; max-width: 40%; color: var(--text-tertiary);
		font-size: var(--text-caption);
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.ta-outcome.bad { color: var(--red); }
	.ta-badge {
		flex-shrink: 0; display: inline-flex; gap: 5px;
		font-family: var(--font-mono); font-size: var(--text-caption); font-weight: 600;
	}
	.ta-badge .add { font-style: normal; color: var(--green); }
	.ta-badge .del { font-style: normal; color: var(--red); }

	.ta-detail {
		padding: 2px 8px 8px 28px;
		display: flex; flex-direction: column; gap: 6px;
		animation: ta-in var(--dur-tight) ease-out;
	}
	.ta-load-state, .ta-load-error {
		font-size: var(--text-footnote); color: var(--text-tertiary);
		display: flex; align-items: center; gap: 6px;
	}
	.ta-load-error { color: var(--red); }
	.ta-retry {
		border: 0; padding: 0; background: none; color: inherit;
		font: inherit; text-decoration: underline; cursor: pointer;
	}
	.ta-section { display: flex; flex-direction: column; gap: 3px; }
	.ta-label {
		font-size: var(--text-caption); font-weight: 600; text-transform: uppercase;
		letter-spacing: 0.05em; color: var(--text-tertiary);
	}
	.ta-code {
		font-family: var(--font-mono); font-size: var(--text-footnote);
		color: var(--text-secondary); background: var(--bg-primary);
		border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
		padding: 6px 10px; margin: 0;
		max-height: 200px; overflow-y: auto;
		white-space: pre-wrap; word-break: break-all;
	}

	.ta-cmd {
		font-family: var(--font-mono); font-size: var(--text-footnote); margin: 0;
		color: var(--text-primary); background: var(--bg-primary);
		border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
		padding: 6px 10px; white-space: pre-wrap; word-break: break-all;
	}
	.ta-cmd-mark { color: var(--green); user-select: none; margin-right: 4px; }
	.ta-well {
		background: var(--bg-primary); border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm); max-height: 220px; overflow: auto;
	}
	.ta-well-pre {
		font-family: var(--font-mono); font-size: var(--text-footnote); margin: 0; padding: 6px 10px;
		color: var(--text-tertiary); white-space: pre-wrap; word-break: break-all;
	}
	.ta-well.read { padding: 4px 0; }
	.ta-rl { display: flex; font-family: var(--font-mono); font-size: var(--text-footnote); line-height: 1.5; }
	.ta-ln {
		flex-shrink: 0; width: 34px; padding-right: 10px; text-align: right;
		color: var(--text-tertiary); opacity: 0.5; user-select: none;
	}
	.ta-lc { flex: 1; min-width: 0; color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; padding-right: 8px; }

	@keyframes ta-in { from { opacity: 0; transform: translateY(-2px); } to { opacity: 1; transform: translateY(0); } }
	@keyframes ta-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

	@media (max-width: 768px) {
		.ta-summary { padding: 10px 12px; }
		.ta-row { padding: 9px 8px; }
	}

</style>
