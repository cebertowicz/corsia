<script lang="ts">
	import { Button } from '$lib/components/ui';

	// "New file" on a Settings › Context level: a text action that opens one name field.
	let { label, busy = false, onCreate }: {
		/** The field's accessible name, naming the level the file is for. */
		label: string;
		busy?: boolean;
		onCreate: (path: string) => Promise<boolean>;
	} = $props();

	let open = $state(false);
	let path = $state('');

	async function create() {
		const next = path.trim();
		if (!next || busy) return;
		if (await onCreate(next)) {
			path = '';
			open = false;
		}
	}
</script>

{#if open}
	<div class="create">
		<!-- svelte-ignore a11y_autofocus -->
		<input
			bind:value={path}
			placeholder="File name (.md)"
			aria-label={label}
			autofocus
			onkeydown={(e) => { if (e.key === 'Enter') void create(); if (e.key === 'Escape') open = false; }}
		/>
		<Button size="sm" disabled={busy || !path.trim()} onclick={create}>Create</Button>
		<Button size="sm" variant="ghost" onclick={() => (open = false)}>Cancel</Button>
	</div>
{:else}
	<Button size="sm" variant="ghost" disabled={busy} onclick={() => (open = true)}>New file</Button>
{/if}

<style>
	.create { flex: 1 1 100%; display: flex; align-items: center; gap: var(--space-2); }
	input {
		flex: 1; min-width: 0;
		font: inherit; font-size: var(--text-subhead);
		padding: var(--space-1) var(--space-3);
		border-radius: var(--radius-sm); border: 1px solid var(--border);
		background: var(--bg-primary); color: var(--text-primary);
	}
</style>
