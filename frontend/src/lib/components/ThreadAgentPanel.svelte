<script lang="ts">
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { threads, demoteAgentLocally, type Thread } from '$lib/stores/threads';
	import { Button } from './ui';
	import { plainResponseError } from '$lib/plainError';

	let { thread }: { thread: Thread | null } = $props();

	let editing = $state(false);
	let busy = $state(false);
	let error = $state('');
	let description = $state('');
	let paths = $state<string[]>([]);
	let domains = $state<string[]>([]);
	let pathDraft = $state('');
	let domainDraft = $state('');
	let loadedFor = $state('');

	const isAgent = $derived(Boolean(thread?.agent_enabled) && Boolean(agentResponsibility(thread).trim()));
	const currentResponsibility = $derived(agentResponsibility(thread));
	const currentPaths = $derived(parseList(thread?.agent_paths));
	const currentDomains = $derived(parseList(thread?.agent_domains));

	$effect(() => {
		const key = `${thread?.id ?? ''}:${thread?.updated_at ?? ''}:${thread?.agent_responsibility ?? ''}:${thread?.agent_paths ?? ''}:${thread?.agent_domains ?? ''}`;
		if (!thread || key === loadedFor) return;
		loadedFor = key;
		description = currentResponsibility;
		paths = currentPaths;
		domains = currentDomains;
		editing = false;
		error = '';
	});

	function agentResponsibility(row: Thread | null): string {
		return row?.agent_responsibility ?? '';
	}

	function parseList(raw: Thread['agent_paths']): string[] {
		if (!raw) return [];
		if (Array.isArray(raw)) return raw.map(String).map((item) => item.trim()).filter(Boolean);
		if (typeof raw === 'string') {
			try {
				const parsed = JSON.parse(raw);
				if (Array.isArray(parsed)) return parsed.map(String).map((item) => item.trim()).filter(Boolean);
			} catch {
				return raw.split(',').map((item) => item.trim()).filter(Boolean);
			}
		}
		return [];
	}

	function updateLocalAgent(fields: Partial<Thread>) {
		if (!thread?.id) return;
		threads.update((rows) => rows.map((row) => (row.id === thread!.id ? { ...row, ...fields } : row)));
	}

	function addToken(kind: 'path' | 'domain') {
		const draft = (kind === 'path' ? pathDraft : domainDraft).trim();
		if (!draft) return;
		if (kind === 'path') {
			if (!paths.includes(draft)) paths = [...paths, draft];
			pathDraft = '';
		} else {
			if (!domains.includes(draft)) domains = [...domains, draft];
			domainDraft = '';
		}
	}

	function removeToken(kind: 'path' | 'domain', value: string) {
		if (kind === 'path') paths = paths.filter((entry) => entry !== value);
		else domains = domains.filter((entry) => entry !== value);
	}

	function beginEdit() {
		description = currentResponsibility;
		paths = currentPaths;
		domains = currentDomains;
		pathDraft = '';
		domainDraft = '';
		error = '';
		editing = true;
	}

	async function saveAgent() {
		const t = get(token);
		const id = thread?.id;
		const trimmed = description.trim();
		if (!t || !id || !trimmed || busy) return;
		busy = true;
		error = '';
		try {
			const res = await fetch(`/api/agents/${id}?token=${encodeURIComponent(t)}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					responsibility: trimmed,
					responsibility_paths: paths,
					responsibility_domains: domains,
					active: true,
				}),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not save this agent. Try again.'));
			updateLocalAgent({
				agent_enabled: 1,
				agent_responsibility: trimmed,
				agent_paths: paths,
				agent_domains: domains,
				agent_status: 'active',
			});
			editing = false;
		} catch (exc) {
			error = exc instanceof Error ? exc.message : "Couldn't save Agent";
		} finally {
			busy = false;
		}
	}

	async function removeAgent() {
		const t = get(token);
		const id = thread?.id;
		if (!t || !id || busy) return;
		busy = true;
		error = '';
		try {
			const res = await fetch(`/api/agents/${id}?token=${encodeURIComponent(t)}`, {
				method: 'DELETE',
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not remove this agent. Try again.'));
			demoteAgentLocally(id);
			editing = false;
		} catch (exc) {
			error = exc instanceof Error ? exc.message : "Couldn't remove Agent";
		} finally {
			busy = false;
		}
	}
</script>

<div class="agent-panel">
	{#if error}
		<div class="agent-error" role="alert">{error}</div>
	{/if}

	{#if editing}
		<div class="agent-form">
			<textarea
				bind:value={description}
				rows="3"
				placeholder="Responsibility"
				aria-label="Responsibility"
			></textarea>

			<div class="token-field">
				<div class="token-label">Paths</div>
				<div class="token-box">
					{#each paths as path (path)}
						<span class="token-chip">{path}<button type="button" aria-label="Remove path {path}" onclick={() => removeToken('path', path)}>x</button></span>
					{/each}
					<input
						bind:value={pathDraft}
						placeholder={paths.length ? '' : 'Folder or file'}
						aria-label="Responsibility path"
						onkeydown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addToken('path'); } }}
					/>
					<button type="button" class="token-add" disabled={!pathDraft.trim()} onclick={() => addToken('path')}>Add</button>
				</div>
			</div>

			<div class="token-field">
				<div class="token-label">Topics</div>
				<div class="token-box">
					{#each domains as domain (domain)}
						<span class="token-chip">{domain}<button type="button" aria-label="Remove topic {domain}" onclick={() => removeToken('domain', domain)}>x</button></span>
					{/each}
					<input
						bind:value={domainDraft}
						placeholder={domains.length ? '' : 'Coordination topic'}
						aria-label="Responsibility topic"
						onkeydown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addToken('domain'); } }}
					/>
					<button type="button" class="token-add" disabled={!domainDraft.trim()} onclick={() => addToken('domain')}>Add</button>
				</div>
			</div>

			<div class="agent-actions">
				<Button size="sm" disabled={busy || !description.trim()} onclick={saveAgent}>Save</Button>
				<Button size="sm" variant="ghost" disabled={busy} onclick={() => (editing = false)}>Cancel</Button>
			</div>
		</div>
	{:else if isAgent}
		<div class="agent-summary">
			<div class="agent-copy">
				<div class="agent-title">{currentResponsibility}</div>
				{#if currentPaths.length || currentDomains.length}
					<div class="agent-tags">
						{#each [...currentPaths, ...currentDomains] as tag (tag)}
							<span>{tag}</span>
						{/each}
					</div>
				{/if}
			</div>
			<div class="agent-actions">
				<Button size="sm" variant="ghost" disabled={busy} onclick={beginEdit}>Edit</Button>
				<Button size="sm" variant="ghost" disabled={busy} onclick={removeAgent}>Remove</Button>
			</div>
		</div>
	{:else}
		<div class="agent-summary">
			<div class="agent-copy">
				<div class="agent-title muted">Not an Agent</div>
			</div>
			<Button size="sm" disabled={busy} onclick={beginEdit}>Make Agent</Button>
		</div>
	{/if}
</div>

<style>
	.agent-panel {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.agent-summary {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-3);
	}

	.agent-copy {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.agent-title {
		color: var(--text-primary);
		font-size: var(--text-subhead);
		font-weight: var(--fw-medium);
		line-height: 1.35;
	}

	.agent-title.muted {
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
	}

	.agent-tags {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		max-height: 74px;
		overflow: auto;
	}

	.agent-tags span,
	.token-chip {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		max-width: 100%;
		border-radius: var(--radius-pill);
		background: var(--bg-secondary);
		color: var(--text-secondary);
		font-size: var(--text-caption);
		padding: 3px 7px;
	}

	.agent-actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-1);
		flex-shrink: 0;
	}

	.agent-form {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.agent-form textarea {
		width: 100%;
		min-width: 0;
		resize: vertical;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-primary);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-subhead);
		padding: var(--space-2);
	}

	.token-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.token-label {
		color: var(--text-tertiary);
		font-size: var(--text-caption);
		font-weight: var(--fw-bold);
		letter-spacing: var(--ls-cap);
		text-transform: uppercase;
	}

	.token-box {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
		max-height: 112px;
		overflow: auto;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-primary);
		padding: 5px;
	}

	.token-box input {
		flex: 1 1 150px;
		min-width: 120px;
		border: 0;
		background: transparent;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-subhead);
		outline: 0;
		padding: 4px;
	}

	.token-chip button,
	.token-add {
		border: 0;
		background: transparent;
		color: var(--text-tertiary);
		cursor: pointer;
		font: inherit;
		padding: 0 2px;
	}

	.token-add {
		color: var(--accent-scope-text);
		font-size: var(--text-caption);
		font-weight: var(--fw-semibold);
		padding: 4px 6px;
	}

	.token-add:disabled {
		color: var(--text-quaternary);
		cursor: not-allowed;
	}

	.agent-form textarea:focus,
	.token-box:focus-within {
		outline: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
		outline-offset: 2px;
	}

	.agent-error {
		border: 1px solid color-mix(in srgb, var(--red) 28%, transparent);
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--red) 8%, transparent);
		color: var(--red);
		font-size: var(--text-footnote);
		padding: var(--space-2);
	}

	@media (max-width: 520px) {
		.agent-summary {
			flex-direction: column;
		}
	}
</style>
