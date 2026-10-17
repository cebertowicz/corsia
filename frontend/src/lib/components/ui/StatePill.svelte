<script lang="ts">
	type State = 'loaded' | 'loading' | 'idle' | 'error' | 'available'
	           | 'default' | 'active' | 'success' | 'warning';

	interface Props {
		state: State;
		label?: string;
		size?: 'sm' | 'md';
		/**
		 * `dot` is approved for setup only: a small dot and the word, as the "Update"
		 * mark under a chat message, with no box. Only setup uses it.
		 */
		look?: 'pill' | 'dot';
	}

	const { state, label, size = 'md', look = 'pill' }: Props = $props();

	const display = $derived(label ?? humanize(state));

	function humanize(s: State): string {
		if (s === 'default') return 'Available';
		return s.charAt(0).toUpperCase() + s.slice(1);
	}
</script>

{#if look === 'dot'}
	<span class="dotmark {state}"><i class="dot" aria-hidden="true"></i>{display}</span>
{:else}
	<span class="pill {state} {size}">{display}</span>
{/if}

<style>
	/* Approved for setup-only use: dot and word. A shared rollout needs a Product Experience decision. */
	.dotmark { display: inline-flex; align-items: center; gap: var(--space-1); font-size: var(--text-caption); color: var(--text-secondary); white-space: nowrap; }
	.dotmark .dot { width: calc(var(--space-1) * 1.5); height: calc(var(--space-1) * 1.5); border-radius: var(--radius-circle); background: var(--text-tertiary); flex: none; }
	.dotmark.loaded .dot, .dotmark.active .dot { background: var(--accent); }
	.dotmark.success .dot { background: var(--green); }
	.dotmark.loading .dot, .dotmark.warning .dot { background: var(--amber); }
	.dotmark.error .dot { background: var(--red); }
	.dotmark.available .dot { background: var(--sky); }

	.pill {
		display: inline-flex; align-items: center;
		font-weight: 600; letter-spacing: 0.04em;
		text-transform: uppercase;
		border: 1px solid;
		border-radius: var(--radius-xs);
		white-space: nowrap;
	}
	.pill.md { font-size: var(--text-caption); padding: 2px 8px; }
	.pill.sm { font-size: var(--text-caption); padding: 1px 6px; }

	.pill.loaded, .pill.active {
		background: var(--accent-wash);
		color: var(--accent-text, var(--accent));
		border-color: var(--accent-line);
	}
	.pill.loading, .pill.warning {
		background: var(--amber-bg);
		color: var(--amber);
		border-color: var(--amber-line);
	}
	.pill.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-color: color-mix(in srgb, var(--red) 30%, transparent);
	}
	.pill.available {
		background: var(--sky-bg);
		color: var(--sky);
		border-color: var(--sky-line);
	}
	.pill.success {
		background: var(--green-bg);
		color: var(--green-text, var(--green));
		border-color: var(--green-border, color-mix(in srgb, var(--green) 30%, transparent));
	}
	.pill.idle, .pill.default {
		background: var(--bg-tertiary);
		color: var(--text-tertiary);
		border-color: var(--border);
	}
</style>
