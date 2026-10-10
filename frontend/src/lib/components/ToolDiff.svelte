<script lang="ts">
	import type { DiffLine } from '$lib/diff';
	import { diffStats } from '$lib/diff';

	/** `words`: break between words, not anywhere in a line, for prose. */
	let { file, lines, words = false }: { file: string; lines: DiffLine[]; words?: boolean } = $props();

	const stats = $derived(diffStats(lines));
	const name = $derived(file ? (file.split('/').pop() || file) : '');
</script>

<div class="diff" class:words>
	<div class="diff-head">
		{#if name}<span class="diff-file" title={file}>{name}</span>{/if}
		<span class="diff-stat add">+{stats[0]}</span>
		<span class="diff-stat del">−{stats[1]}</span>
	</div>
	<div class="diff-body">
		{#each lines as line, i (i)}
			<div class="diff-line {line.type}">
				<span class="diff-gutter">{line.type === 'add' ? '+' : line.type === 'del' ? '−' : ' '}</span>
				<span class="diff-text">{line.text || ' '}</span>
			</div>
		{/each}
	</div>
</div>

<style>
	.diff {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		overflow: hidden;
		background: var(--bg-primary);
	}
	.diff-head {
		display: flex; align-items: center; gap: 8px;
		padding: 5px 10px;
		border-bottom: 1px solid var(--border-subtle);
		background: var(--bg-secondary);
	}
	.diff-file {
		flex: 1; min-width: 0;
		font-family: var(--font-mono); font-size: var(--text-footnote);
		color: var(--text-secondary);
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.diff-stat { font-family: var(--font-mono); font-size: var(--text-caption); font-weight: 600; }
	.diff-stat.add { color: var(--green); }
	.diff-stat.del { color: var(--red); }
	.diff-body {
		max-height: 240px; overflow-y: auto;
		font-family: var(--font-mono); font-size: var(--text-footnote); line-height: 1.55;
	}
	.diff-line { display: flex; }
	.diff-line.add { background: color-mix(in srgb, var(--green) 10%, transparent); }
	.diff-line.del { background: color-mix(in srgb, var(--red) 9%, transparent); }
	.diff-gutter {
		flex-shrink: 0; width: 18px; text-align: center;
		color: var(--text-tertiary); user-select: none;
	}
	.diff-line.add .diff-gutter { color: var(--green); }
	.diff-line.del .diff-gutter { color: var(--red); }
	.diff-text {
		flex: 1; min-width: 0;
		white-space: pre-wrap; word-break: break-all;
		color: var(--text-secondary);
		padding-right: 8px;
	}
	.diff-line.add .diff-text { color: var(--text-primary); }
	/* Prose breaks between words; one unbreakable token still wraps rather than widening the card. */
	.words .diff-text { word-break: normal; overflow-wrap: anywhere; }
</style>
