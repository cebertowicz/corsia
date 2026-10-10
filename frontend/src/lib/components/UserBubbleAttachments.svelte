<script lang="ts">
	import { token } from '$lib/stores/auth';

	/** The attachment strip inside a user bubble. Presentational only. */
	let {
		attachments,
		onOpenLightbox,
	}: {
		attachments: any[];
		onOpenLightbox: (url: string) => void;
	} = $props();

	let failedPreviewIds = $state<string[]>([]);

	function attachmentKey(att: any): string {
		return String(att.file_id ?? att.url ?? att.filename ?? '');
	}

	function previewFailed(att: any): boolean {
		return failedPreviewIds.includes(attachmentKey(att));
	}

	function markPreviewFailed(att: any): void {
		const key = attachmentKey(att);
		if (!failedPreviewIds.includes(key)) {
			failedPreviewIds = [...failedPreviewIds, key];
		}
	}

	function downloadAttachment(att: any) {
		const link = document.createElement('a');
		link.href = att.url + '?token=' + $token;
		link.download = att.filename;
		link.target = '_blank';
		document.body.appendChild(link);
		link.click();
		document.body.removeChild(link);
	}

	function isPdf(att: any): boolean {
		return att.mime_type === 'application/pdf'
			|| Boolean(att.filename?.toLowerCase().endsWith('.pdf'));
	}
</script>

<div class="bubble-attachments">
	{#each attachments as att}
		{#if att.is_image && !previewFailed(att)}
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="bubble-img-link" onclick={() => onOpenLightbox(att.url + '?token=' + $token)}>
				<img src="{att.url}?token={$token}" alt={att.filename} class="bubble-img" onerror={() => markPreviewFailed(att)} />
			</div>
		{:else if att.is_image}
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="bubble-file bubble-image" aria-label="Image attachment" onclick={() => downloadAttachment(att)}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
				<span>{att.filename}</span>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
			</div>
		{:else if isPdf(att)}
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="bubble-file bubble-pdf" onclick={() => onOpenLightbox(att.url + '?token=' + $token)}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="15" y2="17"/><polyline points="9 9 10 9 12 9"/></svg>
				<span>{att.filename}</span>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
			</div>
		{:else}
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="bubble-file" onclick={() => downloadAttachment(att)}>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
				<span>{att.filename}</span>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
			</div>
		{/if}
	{/each}
</div>

<style>
	.bubble-attachments { display: flex; flex-direction: column; gap: 6px; margin-bottom: 6px; }
	.bubble-img-link { display: block; cursor: pointer; }
	.bubble-img {
		width: min(100%, 280px); max-height: 280px; border-radius: var(--radius-sm);
		object-fit: cover; display: block; min-width: 120px;
	}
	.bubble-file {
		display: flex; align-items: center; gap: 8px;
		font-size: var(--text-subhead); color: white; cursor: pointer; padding: 10px 14px;
		background: rgba(0,0,0,0.2); border-radius: var(--radius-sm); transition: background var(--dur-tight);
	}
	.bubble-file:hover { background: rgba(0,0,0,0.3); }
	.bubble-file span { flex: 1; font-weight: 500; }
	.bubble-file svg { opacity: 0.7; flex-shrink: 0; }
	.bubble-pdf { background: rgba(0,0,0,0.25); }
	.bubble-pdf:hover { background: rgba(0,0,0,0.4); }
</style>
