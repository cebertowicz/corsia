<script lang="ts">
	// One artifact row in the thread pane: a typed glyph, a name, and either an
	// action or an explicit disabled reason — never an apparently live inert row.
	// The backend supplies the capability; this component resolves what that
	// capability can actually do here (a local path to open, a derivable URL, a
	// signed-in user for a download) and degrades honestly when it cannot.
	import { get } from 'svelte/store';
	import { token, user } from '$lib/stores/auth';
	import { IconButton } from '$lib/components/ui';
	import { openArtifactTab } from '$lib/stores/artifactWorkspace';
	import { paneOpen, isPaneDrawer } from '$lib/stores/paneOpen';
	import { toasts } from '$lib/stores/toast';
	import { relLabel } from '$lib/time';
	import type { ArtifactNode } from '$lib/api/artifactPane';
	import ArtifactGlyph from './ArtifactGlyph.svelte';
	import { openExternalUrl } from '$lib/linkTargetResolver';

	let { item, threadId = null, pending = false, onHide, onPin }: {
		item: ArtifactNode;
		threadId?: string | null;
		pending?: boolean;
		onHide: (artifactId: string) => void;
		onPin: (artifactId: string, pinned: boolean) => void;
	} = $props();

	// PR artifacts store no URL — only the canonical '<owner>/<repo>#<N>' external_id.
	function githubPrUrl(externalId: string): string | null {
		const m = /^([^/]+)\/([^#]+)#(\d+)$/.exec(externalId);
		return m ? `https://github.com/${m[1]}/${m[2]}/pull/${m[3]}` : null;
	}
	// Uploads are served at the same path register_upload/serve_upload agree on.
	function uploadDownloadUrl(it: ArtifactNode): string | null {
		const uid = get(user)?.id;
		if (!uid) return null;
		const filename = it.title || it.name;
		return `/api/uploads/${encodeURIComponent(uid)}/${encodeURIComponent(it.external_id)}/${encodeURIComponent(filename)}`;
	}
	function resolvedHref(it: ArtifactNode): string | null {
		if (it.action.capability === 'external-link') return githubPrUrl(it.external_id);
		if (it.action.capability === 'download') return uploadDownloadUrl(it);
		return null;
	}

	const disabled = $derived(effectiveDisabled(item));
	const disabledReason = $derived(effectiveDisabledReason(item));

	function effectiveDisabled(it: ArtifactNode): boolean {
		if (it.action.disabled) return true;
		if (it.action.capability === 'leaf-package') return !(it.path && it.path.startsWith('/'));
		if (it.action.capability === 'external-link' || it.action.capability === 'download') {
			return !resolvedHref(it);
		}
		return false;
	}
	function effectiveDisabledReason(it: ArtifactNode): string | null {
		if (it.action.disabled_reason) return it.action.disabled_reason;
		if (!effectiveDisabled(it)) return null;
		if (it.action.capability === 'leaf-package') return 'No local file to open.';
		if (it.action.capability === 'external-link') return 'Could not determine the linked destination.';
		if (it.action.capability === 'download') return 'Sign in again to download this file.';
		return null;
	}

	function activate() {
		const it = item;
		if (effectiveDisabled(it)) return;
		if (it.action.capability === 'leaf-package') {
			if (!threadId) return;
			openArtifactTab(threadId, { artifactId: it.id, path: it.path!, title: it.name });
			// This widget lives in the right drawer on narrow canvases; leaving it open
			// would cover the document the tap just opened.
			if (isPaneDrawer()) paneOpen.set(false);
			return;
		}
		const href = resolvedHref(it);
		if (!href) return;
		if (it.action.capability === 'download') {
			const a = document.createElement('a');
			a.href = `${href}?token=${get(token) ?? ''}`;
			a.download = it.title || it.name;
			a.target = '_blank';
			document.body.appendChild(a);
			a.click();
			a.remove();
			return;
		}
		// A refused opener must not vanish: the same toast the markdown link path shows.
		openExternalUrl(href).catch(() => toasts.show("Couldn't open the external link", 'error'));
	}
</script>

<div class="row-outer">
	{#if !disabled}
		<button type="button" class="row openable" onclick={activate}>
			{@render rowContent()}
		</button>
	{:else}
		<div class="row disabled" title={disabledReason ?? undefined}>
			{@render rowContent()}
		</div>
	{/if}
	{#if item.pin_eligible}
		<IconButton
			ariaLabel={item.pinned ? 'Unpin from pane' : 'Pin to pane'}
			title={item.pinned
				? (item.retirement_exempt
					? 'Unpin — this one is not cleared by task completion'
					: 'Unpin — let this clear once its work is done')
				: (item.retirement_exempt
					? 'Pin — keep this row leading its list'
					: 'Pin — keep this after the task finishes')}
			size="sm"
			variant="ghost"
			class={item.pinned ? 'pin-btn is-pinned' : 'pin-btn'}
			disabled={pending}
			onclick={() => onPin(item.id, !item.pinned)}
		>
			{@render pinIcon(item.pinned)}
		</IconButton>
	{/if}
	<IconButton
		ariaLabel="Remove from pane"
		title="Remove from pane"
		size="sm"
		variant="ghost"
		class="hide-btn"
		disabled={pending}
		onclick={() => onHide(item.id)}
	>
		<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
	</IconButton>
</div>

{#snippet pinIcon(filled: boolean)}
	<svg viewBox="0 0 24 24" width="13" height="13" fill={filled ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3l6 6-3 1-4 4-1 6-2-2-4 4-1-1 4-4-2-2 6-1 4-4z"/></svg>
{/snippet}

{#snippet rowContent()}
	<ArtifactGlyph icon={item.presentation.icon} />
	<span class="name" title={item.name}>{item.name}</span>
	{#if item.reply_waiting}
		<svg class="reply-mark" role="img" aria-label="Reply waiting" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M8 10h8M8 14h5"/></svg>
	{/if}
	{#if item.pinned}
		<span
			class="pin-mark"
			title={item.retirement_exempt
				? 'Pinned — stays in the pane and sorts first'
				: 'Pinned — kept after the task finishes'}
			aria-hidden="true"
		>
			{@render pinIcon(true)}
		</span>
	{/if}
	<span class="sp"></span>
	{#if disabled && disabledReason}
		<span class="meta reason">{disabledReason}</span>
	{:else if item.updated_at}
		<span class="meta">{relLabel(item.updated_at)}</span>
	{/if}
{/snippet}

<style>
	.row-outer { display: flex; align-items: center; gap: 0; }
	.row-outer:hover :global(.hide-btn), .row-outer:focus-within :global(.hide-btn) { opacity: 1; }
	:global(.hide-btn) { opacity: 0; flex-shrink: 0; transition: opacity var(--dur-tight) ease; }
	@media (hover: none) { :global(.hide-btn) { opacity: 1; } }

	/* The pin control is an affordance on hover, but a pinned row keeps it
	   visible: the filled pin beside the name is status, and a control that
	   vanished would leave no way back. */
	.row-outer:hover :global(.pin-btn), .row-outer:focus-within :global(.pin-btn) { opacity: 1; }
	:global(.pin-btn) { opacity: 0; flex-shrink: 0; transition: opacity var(--dur-tight) ease; }
	:global(.pin-btn.is-pinned) { opacity: 1; color: var(--accent); }
	@media (hover: none) { :global(.pin-btn) { opacity: 1; } }

	.pin-mark { display: inline-flex; align-items: center; flex-shrink: 0; color: var(--accent); }

	.row {
		position: relative; display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0;
		padding: 7px 8px; margin-bottom: 1px; border-radius: var(--radius-sm, 8px);
		font: inherit; font-size: var(--text-subhead); color: inherit;
		background: none; border: 0; width: 100%; text-align: left;
	}
	.row.openable { cursor: pointer; }
	.row.openable:hover { background: var(--bg-hover); }
	.row.disabled { color: var(--text-tertiary); cursor: default; }

	.name { font-weight: 500; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
	.row.disabled .name { color: var(--text-tertiary); }
	.reply-mark { flex-shrink: 0; color: var(--accent); }
	.sp { flex: 1; }

	.meta { font-size: var(--text-footnote); color: var(--text-tertiary); font-variant-numeric: tabular-nums; white-space: nowrap; flex-shrink: 0; }
	.meta.reason { font-style: italic; overflow: hidden; text-overflow: ellipsis; max-width: 40%; }
</style>
