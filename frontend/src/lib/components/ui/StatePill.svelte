<script lang="ts">
	type State = 'loaded' | 'loading' | 'idle' | 'error' | 'available'
	           | 'default' | 'active' | 'success' | 'warning';

	interface Props {
		state: State;
		label?: string;
		size?: 'sm' | 'md';
	}

	const { state, label, size = 'md' }: Props = $props();

	const display = $derived(label ?? humanize(state));

	function humanize(s: State): string {
		if (s === 'default') return 'Available';
		return s.charAt(0).toUpperCase() + s.slice(1);
	}
</script>

<span class="pill {state} {size}">{display}</span>

<style>
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
		background: var(--violet-bg);
		color: var(--violet-text, var(--violet));
		border-color: var(--violet-line);
	}
	.pill.loading, .pill.warning {
		background: var(--amber-bg);
		color: var(--amber);
		border-color: var(--amber-line);
	}
	.pill.error {
		background: var(--red-bg);
		color: var(--red);
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
