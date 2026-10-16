<script lang="ts">
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { plainResponseError } from '$lib/plainError';
	import SettingsRow from './SettingsRow.svelte';
	import SettingsContextCreate from './SettingsContextCreate.svelte';

	// One project's level on Settings › Context: the markdown files in its context/.
	let { projectId }: { projectId: string } = $props();

	let files = $state<string[]>([]);
	let loading = $state(true);
	let error = $state('');
	let busy = $state(false);

	$effect(() => { void load(projectId); });

	async function load(id: string) {
		const t = get(token);
		if (!t) return;
		loading = true;
		error = '';
		try {
			const res = await fetch(`/api/projects/${encodeURIComponent(id)}?token=${encodeURIComponent(t)}`);
			if (!res.ok) throw new Error("Couldn't load this project's context");
			const body = await res.json();
			files = Array.isArray(body.context_files) ? body.context_files.map(String) : [];
		} catch (e) {
			error = e instanceof Error ? e.message : "Couldn't load this project's context";
		} finally {
			loading = false;
		}
	}

	function fileName(path: string): string {
		return path.replace(/\\/g, '/').split('/').filter(Boolean).at(-1) ?? path;
	}

	async function create(path: string): Promise<boolean> {
		const t = get(token);
		if (!t || busy) return false;
		busy = true;
		error = '';
		try {
			const res = await fetch('/api/context/bootloader/file', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${t}` },
				body: JSON.stringify({ scope: 'project', project_id: projectId, path, content: `# ${path}\n` }),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not create that. Try again.'));
			const body = await res.json();
			if (Array.isArray(body.files)) files = body.files.map(String);
			return true;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not create that. Try again.';
			return false;
		} finally {
			busy = false;
		}
	}
</script>

{#if error}<p class="note error" role="alert">{error}</p>{/if}
{#if loading}
	<p class="note">Loading…</p>
{:else}
	{#each files as path (path)}
		<SettingsRow name={fileName(path)} />
	{:else}
		<p class="note">No context files for this project yet.</p>
	{/each}
{/if}
<div class="actions">
	<SettingsContextCreate label="New context file for this project" {busy} onCreate={create} />
</div>

<style>
	.note { margin: 0; padding: 0 var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }
	.error { color: var(--red-text); }
	.actions { display: flex; margin-top: var(--space-1); }
</style>
