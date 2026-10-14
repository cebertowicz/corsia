<script lang="ts">
	import { pushBackLayer } from '$lib/historyBackLayer';

	let { url = $bindable(), open = $bindable() }: {
		url: string;
		open: boolean;
	} = $props();

	const isPdf = $derived(url.toLowerCase().includes('.pdf') || url.toLowerCase().includes('application/pdf'));

	function close() { open = false; url = ''; }

	function closeFromBackdrop(e: MouseEvent) {
		if (e.target === e.currentTarget) close();
	}

	$effect(() => {
		if (!open) return;
		return pushBackLayer(() => {
			close();
			return true;
		});
	});
</script>

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="lightbox-overlay" onclick={closeFromBackdrop}>
		<button class="lightbox-close" onclick={close} aria-label="Close">
			<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
		</button>
		{#if isPdf}
			<div class="lightbox-pdf-container">
				<iframe
					src={url}
					class="lightbox-pdf"
					title="PDF viewer"
					sandbox="allow-scripts allow-same-origin"
				></iframe>
				<a class="pdf-download-btn" href={url} download target="_blank">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
					Download PDF
				</a>
			</div>
		{:else}
			<img src={url} alt="Full size" class="lightbox-img" />
		{/if}
	</div>
{/if}

<style>
	.lightbox-overlay {
		position: fixed; inset: 0; z-index: 2000;
		background: rgba(0,0,0,0.92);
		display: flex; align-items: center; justify-content: center;
		cursor: pointer;
		-webkit-backdrop-filter: var(--glass-blur-soft); backdrop-filter: var(--glass-blur-soft);
	}
	.lightbox-close {
		position: absolute; top: var(--safe-top, 12px); right: 12px;
		margin-top: 12px;
		background: rgba(255,255,255,0.15); border: none; border-radius: var(--radius-circle);
		width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
		color: white; cursor: pointer; z-index: 2001;
	}
	.lightbox-close:hover { background: rgba(255,255,255,0.25); }
	.lightbox-img {
		max-width: 95vw; max-height: 90vh; object-fit: contain;
		border-radius: var(--radius-2xs); cursor: default;
	}
	.lightbox-pdf-container {
		display: flex; flex-direction: column; align-items: stretch;
		width: min(95vw, 900px); height: 90vh;
		cursor: default;
	}
	.lightbox-pdf {
		flex: 1; border: none; border-radius: 4px 4px 0 0;
		background: white;
		min-height: 0;
	}
	.pdf-download-btn {
		display: flex; align-items: center; justify-content: center; gap: 8px;
		padding: 10px 16px; background: rgba(255,255,255,0.15);
		color: white; text-decoration: none; font-size: var(--text-subhead); font-weight: 500;
		border-radius: 0 0 4px 4px; transition: background var(--dur-tight);
		flex-shrink: 0;
	}
	.pdf-download-btn:hover { background: rgba(255,255,255,0.25); }

	@media (max-width: 768px) {
		.lightbox-pdf-container {
			width: 100vw; height: 100dvh;
			border-radius: 0;
		}
		.lightbox-pdf {
			border-radius: 0;
		}
		.pdf-download-btn {
			border-radius: 0;
			padding: 14px 16px;
		}
	}
</style>
