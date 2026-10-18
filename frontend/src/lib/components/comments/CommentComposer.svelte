<script lang="ts">
	import { untrack } from 'svelte';
	import { autosize } from '$lib/actions/autosize';
	import Field from '$lib/components/ui/Field.svelte';

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
	let posting = $state(false);
	$effect(() => {
		text;
		onBody?.(text);
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
	<Field variant="nested" hideLabel label="Comment" labelFor={`${explanationId}-body`}>
		<textarea id={`${explanationId}-body`} data-drawer-autofocus rows="1" placeholder="Comment…" bind:value={text} use:autosize={text} onkeydown={onKey}></textarea>
	</Field>
	<div class="actions">
		<button class="cancel" onclick={onCancel}>Cancel</button>
		<button class="submit" disabled={!text.trim() || posting || !!disabledReason} aria-describedby={disabledReason ? explanationId : undefined} onclick={() => void submit()}>{posting ? 'Posting…' : 'Comment'}</button>
	</div>
	{#if disabledReason}<p id={explanationId} class="selection-reason">{disabledReason}</p>{/if}
</div>

<style>
	.composer { display: flex; flex-direction: column; gap: 8px; }
	.quote { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border-left: 2px solid color-mix(in srgb, var(--amber) 55%, transparent); padding-left: 8px; color: var(--text-secondary); font-size: var(--text-caption); font-style: italic; }
	.quote.label { font-style: normal; }
	.actions { display: flex; justify-content: flex-end; gap: 8px; }
	button { font-size: var(--text-caption); font-weight: var(--fw-semibold); cursor: pointer; }
	.cancel { padding: 5px 8px; border: none; background: none; color: var(--text-secondary); }
	.submit { padding: 6px 14px; border: none; border-radius: var(--radius-sm); background: var(--accent-fill); color: var(--accent-on-fill); }
	.submit:disabled { opacity: .4; cursor: default; }
	.selection-reason { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
</style>
