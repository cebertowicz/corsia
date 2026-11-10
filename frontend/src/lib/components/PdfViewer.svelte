<script lang="ts">
	// Shows a workspace PDF inside the artifact leaf: the file's bytes from the authenticated file route, checked to be a PDF,
	// then handed to the browser's own PDF viewer in a frame. No comments and no editor (prototype: the backend does not
	// serve PDF bytes on that route yet; see the email draft view document).
	import { untrack } from 'svelte';
	import { imageContentUrl } from '$lib/artifactImage';
	import { token } from '$lib/stores/auth';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { TabLoadSignal } from '$lib/artifactTabs';
	import Button from '$lib/components/ui/Button.svelte';

	let { target, onSignal, onClose, backToDraft = false }: {
		target: DocViewerTarget;
		onSignal?: (signal: TabLoadSignal) => void;
		/** Closes this tab. With `backToDraft` it first selects the draft the file was opened from, so closing is going back to it. */
		onClose?: () => void;
		/** The file was opened from an email draft's attachment chip and that draft's tab is still open; only then the failure says Back to draft. */
		backToDraft?: boolean;
	} = $props();

	let pdfState = $state<'loading' | 'ready' | 'error'>('loading');
	let src = $state<string | null>(null);
	let loadSeq = 0;
	const targetKey = $derived(`${target.instanceId}\u0000${target.path}\u0000${target.projectId ?? ''}`);

	$effect(() => {
		targetKey;
		untrack(() => {
			const d = target;
			const seq = ++loadSeq;
			const signal = (s: Omit<TabLoadSignal, 'instanceId'>) => onSignal?.({ ...s, instanceId: d.instanceId });
			pdfState = 'loading';
			src = null;
			signal({ status: 'loading' });
			fetch(imageContentUrl({ path: d.path, projectId: d.projectId, token: $token ?? '' }))
				.then(async (response) => {
					if (seq !== loadSeq) return;
					if (response.status === 404) return void signal({ status: 'unavailable' });
					const bytes = response.ok ? await response.arrayBuffer() : null;
					// A file that is not a PDF, or a route that sent something else, is a failure, never raw text in the frame.
					if (!bytes || new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('not a pdf');
					if (seq !== loadSeq) return;
					src = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' }));
					pdfState = 'ready';
					signal({ status: 'ready' });
				})
				.catch(() => {
					if (seq !== loadSeq) return;
					pdfState = 'error';
					signal({ status: 'error' });
				});
		});
		return () => { if (src) URL.revokeObjectURL(src); };
	});
</script>

<div class="pdf-viewer">
	{#if pdfState === 'loading'}
		<div class="loadbar"><span></span></div>
	{:else if pdfState === 'error'}
		<div class="load-error" role="alert">
			<p>Couldn’t show this PDF.</p>
			<span>{backToDraft ? 'The file may be damaged or unreadable. Go back to the draft, where you can remove the attachment.' : 'The file may be damaged or unreadable.'}</span>
			<Button size="sm" variant="secondary" onclick={() => onClose?.()}>{backToDraft ? 'Back to draft' : 'Close'}</Button>
		</div>
	{:else if src}
		<iframe class="frame" title={target.name} {src}></iframe>
	{/if}
</div>

<style>
	.pdf-viewer { position: relative; height: 100%; min-height: 0; display: flex; flex-direction: column; box-sizing: border-box; }
	.frame { flex: 1; min-height: 0; width: 100%; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-secondary); }
	.load-error { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-2); padding: var(--space-6) var(--space-1); color: var(--text-secondary); font-size: var(--text-footnote); }
	.load-error p { margin: 0; font-size: var(--text-subhead); color: var(--red); font-weight: var(--fw-medium); }
</style>
