<script lang="ts">
	import { untrack } from 'svelte';
	import { resizeTextInputToContent } from '$lib/textInputResize';

	let { quote, label = '', body = '', disabledReason = '', onBody, onSubmit, onCancel }: {
		quote: string;
		/** What the comment is on when it is not text, such as a box on an image; shown in place of the quote. */
		label?: string;
		body?: string;
		disabledReason?: string;
		onBody?: (text: string) => void;
		onSubmit: (text: string) => Promise<void>;
		onCancel: () => void;
	} = $props();
	let text = $state(untrack(() => body));
	const explanationId = $props.id();
	let textarea = $state<HTMLTextAreaElement>();
	let posting = $state(false);
	$effect(() => {
		text;
		onBody?.(text);
		if (textarea) resizeTextInputToContent(textarea, 150);
	});
	const snippet = (value: string) => value.length > 64 ? value.slice(0, 64) + '…' : value;
	async function submit() {
		const value = text.trim();
		if (!value || posting || disabledReason) return;
		posting = true;
		try { await onSubmit(value); text = ''; }
		catch { /* onSubmit reports its own failure; keep the draft for a retry. */ }
		finally { posting = false; }
	}
	function onKey(event: KeyboardEvent) {
		if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') void submit();
		if (event.key === 'Escape') onCancel();
	}
</script>

<div class="composer">
	<div class="quote" class:label>{label || `“${snippet(quote)}”`}</div>
	<textarea data-drawer-autofocus rows="2" placeholder="Comment…" bind:this={textarea} bind:value={text} onkeydown={onKey}></textarea>
	<div class="actions">
		<button class="cancel" onclick={onCancel}>Cancel</button>
		<button class="submit" disabled={!text.trim() || posting || !!disabledReason} aria-describedby={disabledReason ? explanationId : undefined} onclick={() => void submit()}>{posting ? 'Posting…' : 'Comment'}</button>
	</div>
	{#if disabledReason}<p id={explanationId} class="selection-reason">{disabledReason}</p>{/if}
</div>

<style>
	.composer { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; border-radius: var(--radius); background: var(--accent-bg); border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent); }
	.quote { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border-left: 2px solid color-mix(in srgb, var(--amber) 55%, transparent); padding-left: 8px; color: var(--text-secondary); font-size: var(--text-caption); font-style: italic; }
	.quote.label { font-style: normal; }
	textarea { box-sizing: border-box; width: 100%; min-height: 48px; max-height: 150px; resize: vertical; padding: 7px 9px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-primary); color: var(--text-primary); font: inherit; font-size: var(--text-footnote); }
	textarea:focus { outline: none; border-color: var(--accent); }
	.actions { display: flex; justify-content: flex-end; gap: 8px; }
	button { font-size: var(--text-caption); font-weight: var(--fw-semibold); cursor: pointer; }
	.cancel { padding: 5px 8px; border: none; background: none; color: var(--text-secondary); }
	.submit { padding: 6px 14px; border: none; border-radius: var(--radius-sm); background: var(--accent); color: #fff; }
	.submit:disabled { opacity: .4; cursor: default; }
	.selection-reason { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
</style>
