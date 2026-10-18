<script lang="ts">
	import type { Snippet } from 'svelte';
	import './text-field.css';

	interface Props {
		label: string;
		labelFor?: string;
		hint?: string;
		error?: string;
		required?: boolean;
		layout?: 'row' | 'stacked';
		/**
		 * `chip`: the control drawn as the composer's chip (glass fill, large radius), label above and no divider, for a
		 * field sitting straight on a pane. `nested`: the same shape with a plain outline and no fill, for a field inside a card or sheet.
		 */
		variant?: 'default' | 'chip' | 'nested';
		/** Keep the label as the control's accessible name but do not draw it. */
		hideLabel?: boolean;
		trailing?: Snippet;
		children: Snippet;
	}

	const {
		label, labelFor, hint, error, required = false,
		layout = 'row', variant = 'default', hideLabel = false, trailing, children,
	}: Props = $props();
</script>

<div class="field {layout}" class:chip={variant === 'chip'} class:nested={variant === 'nested'} class:hide-label={hideLabel} class:has-error={!!error}>
	{#if labelFor}
		<label class="l" for={labelFor}>{label}{#if required}<span class="req" aria-hidden="true">*</span>{/if}</label>
	{:else}
		<span class="l">{label}{#if required}<span class="req" aria-hidden="true">*</span>{/if}</span>
	{/if}
	<div class="c">
		{@render children()}
	</div>
	{#if trailing}
		<span class="t">{@render trailing()}</span>
	{:else}
		<span class="t"></span>
	{/if}
	{#if hint && !error}
		<span class="hint">{hint}</span>
	{/if}
	{#if error}
		<span class="err">{error}</span>
	{/if}
</div>

<style>
	.field {
		display: grid;
		grid-template-columns: 140px 1fr auto;
		grid-template-areas:
			'label control trailing'
			'.     msg     msg';
		align-items: center;
		gap: 6px 14px;
		padding: 10px 0;
		border-bottom: 1px solid var(--border-subtle);
	}
	.field:last-child { border-bottom: 0; }
	.field.stacked {
		grid-template-columns: 1fr;
		grid-template-areas:
			'label'
			'control'
			'trailing'
			'msg';
		gap: 6px;
	}

	.l {
		grid-area: label;
		font-size: var(--text-subhead);
		color: var(--text-secondary);
	}
	.req { color: var(--red-text); margin-left: 2px; }

	.c {
		grid-area: control;
		display: flex; align-items: center; gap: 8px;
		min-width: 0;
	}
	.c :global(input),
	.c :global(select),
	.c :global(textarea) {
		background: var(--bg-tertiary);
		border: 1px solid var(--border);
		border-radius: var(--radius-xs);
		color: var(--text-primary);
		padding: 0 10px;
		font-size: var(--text-subhead);
		font-family: inherit;
		width: 100%;
		min-width: 0;
		height: 32px;
		line-height: 1;
		appearance: none;
		-webkit-appearance: none;
	}
	.c :global(textarea) {
		height: auto;
		min-height: 32px;
		padding: 8px 10px;
		line-height: 1.5;
		resize: vertical;
	}
	.c :global(input:focus),
	.c :global(select:focus),
	.c :global(textarea:focus) {
		outline: none;
		border-color: var(--accent);
		box-shadow: var(--shadow-focus);
	}

	.c :global(select) {
		padding-right: 30px;
		background-image: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239ca3af' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 10px center;
		background-size: 12px;
		cursor: pointer;
	}

	.c :global(input[type="number"]) {
		-moz-appearance: textfield;
	}
	.c :global(input[type="number"]::-webkit-outer-spin-button),
	.c :global(input[type="number"]::-webkit-inner-spin-button) {
		-webkit-appearance: none;
		margin: 0;
	}

	.t {
		grid-area: trailing;
		font-size: var(--text-footnote); color: var(--text-tertiary);
	}

	.hint {
		grid-area: msg;
		font-size: var(--text-footnote); color: var(--text-tertiary);
	}
	.err {
		grid-area: msg;
		font-size: var(--text-footnote); color: var(--red-text);
	}

	/* chip and nested: label above, no divider; the control's own look is in text-field.css. */
	.field.chip, .field.nested { border-bottom: 0; padding: 0; grid-template-columns: 1fr; grid-template-areas: 'label' 'control' 'msg'; gap: var(--space-1); }
	.field.chip.hide-label, .field.nested.hide-label { grid-template-areas: 'control' 'msg'; }
	.field.chip .t, .field.nested .t { display: none; }
	.field.chip .l, .field.nested .l { font-size: var(--text-footnote); color: var(--text-secondary); padding-left: var(--space-1); }
	.field.hide-label .l {
		position: absolute; inline-size: 0; block-size: 0; margin: 0; padding: 0; overflow: hidden;
		clip-path: inset(50%); white-space: nowrap;
	}
	.field.chip .c :global(select) { width: auto; flex: none; padding-right: calc(var(--space-6) + var(--space-3)); background-position: right var(--space-4) center; }
	.field.chip .c :global(input::placeholder) { color: var(--text-tertiary); }
	.field.chip .c :global(input:focus),
	.field.chip .c :global(select:focus) {
		border-color: var(--accent); box-shadow: var(--glass-rim), var(--shadow-chip), var(--shadow-focus);
	}
	.field.chip.has-error .c :global(input) { border-color: var(--red-text); }
	.field.chip .hint, .field.chip .err, .field.nested .hint, .field.nested .err { padding-left: var(--space-1); }

	@media (max-width: 720px) {
		.field {
			grid-template-columns: 1fr;
			grid-template-areas:
				'label'
				'control'
				'trailing'
				'msg';
		}
		.field > * { justify-self: start; width: 100%; }
	}
	@media (max-width: 480px) {
		.field { padding: 8px 0; }
		.l { font-size: var(--text-subhead); }
		.c :global(input),
		.c :global(select),
		.c :global(textarea) { padding-left: 8px; padding-right: 8px; }
		.c :global(select) { padding-right: 28px; }
	}
</style>
