<script lang="ts">
	import ArtifactPane from '$lib/components/ArtifactPane.svelte';

	const demo = {
		nodes: [
			{
				kind: 'artifact', id: 'a1', integration: 'workspace', type: 'document',
				external_id: 'comment-schema.md', title: null, name: 'comment-schema.md',
				path: '/workspace/comment-schema.md', retention: 'durable', surfacing: 'visible', pinned: false, pin_eligible: true, retirement_exempt: false,
				state: 'active', updated_at: new Date(Date.now() - 5 * 60_000).toISOString(),
				description: null, metadata: {},
				presentation: { label: 'Document', icon: 'file-text', capability: 'leaf-package' },
				action: { capability: 'leaf-package', disabled: false, disabled_reason: null }
			},
			{
				kind: 'worktree-group', key: 'wt-1', label: 'feat/foo · 8 files', branch: 'feat/foo',
				file_count: 2, attention: false,
				children: [
					{
						kind: 'artifact', id: 'a2', integration: 'workspace', type: 'code',
						external_id: 'backend/api/files.py', title: null, name: 'files.py',
						path: '/workspace/.worktrees/feat-foo/backend/api/files.py', retention: 'ephemeral',
						surfacing: 'visible', pinned: false, pin_eligible: true, retirement_exempt: false, state: 'active',
						updated_at: new Date(Date.now() - 30 * 60_000).toISOString(), description: null,
						metadata: {},
						presentation: { label: 'Code', icon: 'file-code', capability: 'leaf-package' },
						action: { capability: 'leaf-package', disabled: false, disabled_reason: null }
					},
					{
						kind: 'artifact', id: 'a3', integration: 'workspace', type: 'code',
						external_id: 'frontend/ArtifactPane.svelte', title: null, name: 'ArtifactPane.svelte',
						path: '/workspace/.worktrees/feat-foo/frontend/ArtifactPane.svelte', retention: 'ephemeral',
						surfacing: 'visible', pinned: false, pin_eligible: true, retirement_exempt: false, state: 'active',
						updated_at: new Date(Date.now() - 45 * 60_000).toISOString(), description: null,
						metadata: {},
						presentation: { label: 'Code', icon: 'file-code', capability: 'leaf-package' },
						action: { capability: 'leaf-package', disabled: false, disabled_reason: null }
					}
				]
			},
			{
				kind: 'artifact', id: 'a4', integration: 'github', type: 'pull-request',
				external_id: 'cebertowicz/corsia#919', title: 'PR #919', name: 'PR #919',
				path: null, retention: 'durable', surfacing: 'visible', pinned: false, pin_eligible: true, retirement_exempt: false, state: 'active',
				updated_at: new Date(Date.now() - 2 * 3600_000).toISOString(), description: null,
				metadata: {},
				presentation: { label: 'Pull Request', icon: 'git-pull-request', capability: 'external-link' },
				action: { capability: 'external-link', disabled: false, disabled_reason: null }
			},
			{
				kind: 'artifact', id: 'a5', integration: 'upload', type: 'upload',
				external_id: 'file-1', title: 'reference.png', name: 'reference.png',
				path: '/uploads/demo/file-1/reference.png', retention: 'durable', surfacing: 'visible', pinned: false, pin_eligible: true, retirement_exempt: false,
				state: 'active', updated_at: new Date(Date.now() - 5 * 3600_000).toISOString(),
				description: null, metadata: {},
				presentation: { label: 'Upload', icon: 'paperclip', capability: 'download' },
				action: { capability: 'download', disabled: false, disabled_reason: null }
			},
			{
				kind: 'artifact', id: 'a6', integration: 'some-connector', type: 'mystery-widget',
				external_id: 'w-1', title: 'mystery widget', name: 'mystery widget',
				path: null, retention: 'durable', surfacing: 'visible', pinned: false, pin_eligible: true, retirement_exempt: false, state: 'active',
				updated_at: new Date(Date.now() - 6 * 3600_000).toISOString(), description: null,
				metadata: {},
				presentation: { label: 'Unsupported', icon: 'file-question', capability: 'unsupported' },
				action: { capability: 'unsupported', disabled: true, disabled_reason: 'No handler is registered for this artifact type.' }
			}
		],
		needs_attention: [
			{ kind: 'repair-failure', artifact_id: 'a1', reason: 'permission denied reading file', retryable: true }
		],
		total: 6
	};
</script>

<svelte:head><title>ArtifactPane preview</title></svelte:head>

<div class="canvas">
	<div class="slot">
		<div class="cap">Artifacts <span class="ct">{demo.total}</span></div>
		<ArtifactPane demo={demo as any} />
	</div>
</div>

<style>
	.canvas {
		min-height: 100vh; display: grid; place-items: center; padding: 48px;
		background:
			radial-gradient(900px 600px at 25% -5%, #20203a 0%, transparent 55%),
			radial-gradient(800px 600px at 110% 40%, #0f2c2e 0%, transparent 50%),
			var(--bg-primary);
	}
	.slot { width: 320px; }
	.cap {
		display: flex; align-items: center; gap: 6px; padding: 4px 8px 8px;
		font-size: var(--text-caption); font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase;
		color: color-mix(in srgb, var(--accent) 55%, var(--text-tertiary));
	}
	.cap .ct { color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
</style>
