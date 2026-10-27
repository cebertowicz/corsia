<script lang="ts">
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { activeThreadId, threads, type Thread } from '$lib/stores/threads';
	import { openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { contextAttachTarget } from '$lib/stores/contextAttach';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import { Button, IconButton } from './ui';
	import { plainResponseError } from '$lib/plainError';

	let {
		threadId = null,
		hiddenThread = null,
		onHiddenThreadFilesChanged,
		onBrowseExisting,
	}: {
		threadId?: string | null;
		hiddenThread?: Pick<Thread, 'id' | 'title' | 'project_id' | 'bootloader_files'> | null;
		onHiddenThreadFilesChanged?: (files: string[]) => void;
		onBrowseExisting?: () => void;
	} = $props();

	let busy = $state(false);
	let error = $state('');
	let newThreadContextPath = $state('');
	let createOpen = $state(false);

	const currentThread = $derived.by(() => {
		const id = threadId ?? $activeThreadId;
		return id ? $threads.find((thread) => thread.id === id) ?? (hiddenThread?.id === id ? hiddenThread : null) : null;
	});
	const currentThreadId = $derived(currentThread?.id ?? threadId ?? $activeThreadId ?? null);
	const threadFiles = $derived(parseFiles(currentThread?.bootloader_files));

	function parseFiles(raw: Thread['bootloader_files']): string[] {
		if (!raw) return [];
		if (Array.isArray(raw)) return raw.map(String);
		if (typeof raw === 'string') {
			try {
				const parsed = JSON.parse(raw);
				return Array.isArray(parsed) ? parsed.map(String) : [];
			} catch {
				return [];
			}
		}
		return [];
	}

	function displayName(path: string): string {
		const parts = path.split('/').filter(Boolean);
		return parts.at(-1) ?? path;
	}

	function updateThreadFiles(files: string[]) {
		const id = currentThreadId;
		if (!id) return;
		if (hiddenThread?.id === id) {
			onHiddenThreadFilesChanged?.(files);
			return;
		}
		threads.update((rows) =>
			rows.map((thread) => (thread.id === id ? { ...thread, bootloader_files: files } : thread)),
		);
	}

	async function requestJson(path: string, init: RequestInit) {
		const t = get(token);
		if (!t) throw new Error('Sign in required');
		const res = await fetch(path, {
			...init,
			headers: {
				Authorization: `Bearer ${t}`,
				'Content-Type': 'application/json',
				...(init.headers ?? {}),
			},
		});
		if (!res.ok) throw new Error(await plainResponseError(res, 'Couldn’t load this chat’s context. Try again.'));
		return res.json();
	}

	async function patchThreadContextFiles(files: string[]) {
		const id = currentThreadId;
		if (!id || busy) return;
		const previous = threadFiles;
		busy = true;
		error = '';
		updateThreadFiles(files);
		try {
			const body = await requestJson('/api/context/bootloader', {
				method: 'PATCH',
				body: JSON.stringify({ scope: 'thread', thread_id: id, files }),
			});
			updateThreadFiles(body.files ?? files);
		} catch (exc) {
			updateThreadFiles(previous);
			error = exc instanceof Error ? exc.message : "Couldn't update thread context";
		} finally {
			busy = false;
		}
	}

	async function createThreadContextFile() {
		const id = currentThreadId;
		const path = newThreadContextPath.trim();
		if (!id || !path || busy) return;
		busy = true;
		error = '';
		try {
			const body = await requestJson('/api/context/bootloader/file', {
				method: 'POST',
				body: JSON.stringify({
					scope: 'thread',
					thread_id: id,
					path,
					content: `# ${path}\n`,
				}),
			});
			newThreadContextPath = '';
			createOpen = false;
			updateThreadFiles(body.files ?? threadFiles);
		} catch (exc) {
			error = exc instanceof Error ? exc.message : "Couldn't create thread context";
		} finally {
			busy = false;
		}
	}

	function browseExisting() {
		const id = currentThreadId;
		if (!id || busy) return;
		contextAttachTarget.set({
			scope: 'thread',
			threadId: id,
			projectId: currentThread?.project_id ?? null,
			targetLabel: currentThread?.title ?? 'Current chat',
			existingFiles: threadFiles,
			returnTo: 'thread-settings',
		});
		onBrowseExisting?.();
	}

	function openFile(path: string) {
		if (!currentThreadId) return;
		threadSettingsOpen.set(false);
		openArtifactAndShowThread({ path, title: displayName(path) }, currentThreadId);
	}
</script>

<div class="thread-context">
	{#if error}
		<div class="ctx-error" role="alert">{error}</div>
	{/if}

	{#if !currentThreadId}
		<div class="ctx-empty">Select a chat to manage context.</div>
	{:else}
		{#if threadFiles.length === 0}
			<div class="ctx-empty">No files pinned to this chat.</div>
		{:else}
			<div class="ctx-files">
				{#each threadFiles as file (file)}
					<div class="ctx-row">
						<button
							type="button"
							class="ctx-file press-tint"
							title={displayName(file)}
							onclick={() => openFile(file)}
						>
							<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
							<span>{displayName(file)}</span>
						</button>
						<IconButton
							size="sm"
							variant="danger"
							title="Remove from chat context"
							ariaLabel={`Remove ${displayName(file)} from chat context`}
							disabled={busy}
							onclick={() => patchThreadContextFiles(threadFiles.filter((entry) => entry !== file))}
						>
							<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
						</IconButton>
					</div>
				{/each}
			</div>
		{/if}

		<div class="ctx-actions">
			<Button
				size="sm"
				disabled={busy}
				onclick={browseExisting}
			>Add existing</Button>
			<Button
				size="sm"
				variant={createOpen ? 'ghost' : 'secondary'}
				disabled={busy}
				onclick={() => (createOpen = !createOpen)}
			>{createOpen ? 'Cancel' : 'Create file'}</Button>
		</div>

		{#if createOpen}
			<div class="ctx-advanced">
				<div class="ctx-create">
					<input
						bind:value={newThreadContextPath}
						placeholder="context/thread-notes.md"
						aria-label="New chat context file"
						onkeydown={(event) => { if (event.key === 'Enter') void createThreadContextFile(); }}
					/>
					<Button
						size="sm"
						disabled={busy || !newThreadContextPath.trim()}
						onclick={createThreadContextFile}
					>Create</Button>
				</div>
			</div>
		{/if}
	{/if}
</div>

<style>
	.thread-context {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.ctx-files {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.ctx-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		gap: var(--space-1);
		min-height: var(--space-7);
	}

	.ctx-file {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		min-width: 0;
		width: 100%;
		border: 0;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-primary);
		cursor: pointer;
		font: inherit;
		font-size: var(--text-subhead);
		padding: var(--space-1) var(--space-2);
		text-align: left;
	}

	.ctx-file svg {
		flex: 0 0 auto;
		color: var(--text-tertiary);
	}

	.ctx-file span {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ctx-actions :global(button:disabled) {
		cursor: not-allowed;
		opacity: 0.5;
	}

	.ctx-actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-1);
	}

	.ctx-advanced {
		padding-top: var(--space-1);
	}
	.ctx-create {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: var(--space-2);
		margin-top: var(--space-2);
	}

	.ctx-create input {
		min-width: 0;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-primary);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-subhead);
		padding: var(--space-1) var(--space-2);
	}

	.ctx-create input:focus,
	.ctx-file:focus-visible {
		outline: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
		outline-offset: 2px;
	}

	.ctx-empty {
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		padding: var(--space-1) 0;
	}

	.ctx-error {
		border: 1px solid color-mix(in srgb, var(--red) 28%, transparent);
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--red) 8%, transparent);
		color: var(--red);
		font-size: var(--text-footnote);
		padding: var(--space-2);
	}
</style>
