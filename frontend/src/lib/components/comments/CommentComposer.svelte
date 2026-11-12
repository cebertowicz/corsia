<script lang="ts">
	import { untrack } from 'svelte';
	import { autosize } from '$lib/actions/autosize';
	import Field from '$lib/components/ui/Field.svelte';
	import CommentQuote from './CommentQuote.svelte';
	import MentionPicker from './MentionPicker.svelte';
	import { createMentionSelection, type MentionOccurrence } from './mentionSelection';
	let input = $state<HTMLTextAreaElement>();
	let picker = $state<MentionPicker>();

	let { quote, label = '', body = '', fieldLabel = 'Comment', placeholder = 'Comment, or @ to tag an agent', disabledReason = '', submitLabel = 'Comment', submittingLabel = 'Posting…', pending = false, error = '', retainText = false, onBody, onSubmit, onCancel, mentions = true, selectedTags = createMentionSelection(), onEscape }: {
		quote: string;
		/** What the comment is on when it is not text, such as a box on an image; shown in place of the quote. */
		label?: string;
		body?: string;
		fieldLabel?: string;
		placeholder?: string;
		disabledReason?: string;
		submitLabel?: string;
		submittingLabel?: string;
		pending?: boolean;
		error?: string;
		retainText?: boolean;
		mentions?: boolean;
		selectedTags?: ReturnType<typeof createMentionSelection>;
		onBody?: (text: string) => void;
		onSubmit: (text: string, taggedAgentIds: string[], mentions: MentionOccurrence[]) => Promise<void>;
		onCancel: () => void;
		/** Escape, when it should close without discarding the text (as a click outside does); Cancel by default. */
		onEscape?: () => void;
	} = $props();
	let text = $state(untrack(() => body));
	const explanationId = $props.id();
	let posting = $state(false);
	$effect(() => {
		text;
		untrack(() => onBody?.(text));
	});
	async function submit() {
		const value = text.trim();
		if (!value || posting || pending || disabledReason) return;
		posting = true;
		const offset = text.length - text.trimStart().length;
		const mentions = (picker?.occurrences(text) ?? []).map(item => ({ ...item, start: item.start - offset, end: item.end - offset }));
		try { await onSubmit(value, [...new Set(mentions.map(item => item.agent_id))], mentions); if (!retainText) { selectedTags.clear(); text = ''; } }
		catch { /* onSubmit reports its own failure; keep the draft for a retry. */ }
		finally { posting = false; }
	}
	function onKey(event: KeyboardEvent) {
		if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') void submit();
		if (event.key === 'Escape') { selectedTags.clear(); (onEscape ?? onCancel)(); }
	}
</script>

<div class="composer">
	<div class="quote-wrap"><CommentQuote {quote} {label} /></div>
	<div class="input-wrap">
		{#if mentions}<MentionPicker bind:this={picker} {input} {selectedTags} />{/if}
		<Field variant="nested" hideLabel label={fieldLabel} labelFor={`${explanationId}-body`}>
			<textarea bind:this={input} id={`${explanationId}-body`} data-drawer-autofocus rows="1" {placeholder} readonly={pending} bind:value={text} use:autosize={text} onkeydown={onKey}></textarea>
		</Field>
	</div>
	<div class="actions">
		<button class="cancel" onclick={() => { selectedTags.clear(); onCancel(); }}>Cancel</button>
		<button class="submit" disabled={!text.trim() || posting || pending || !!disabledReason} aria-describedby={disabledReason ? explanationId : undefined} onclick={() => void submit()}>{posting || pending ? submittingLabel : error ? 'Retry' : submitLabel}</button>
	</div>
	{#if disabledReason}<p id={explanationId} class="selection-reason">{disabledReason}</p>{/if}
	{#if error}<p class="selection-reason" role="alert">{error}</p>{/if}
</div>

<style>
	.composer { display: flex; flex-direction: column; min-height: 0; gap: 8px; }
	.quote-wrap { flex: none; }
	.input-wrap { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
	.input-wrap :global(.field) { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; align-items: stretch; }
	.input-wrap :global(.c) { flex: 1 1 auto; flex-direction: column; min-height: 0; align-items: stretch; }
	.input-wrap textarea { flex: 1 1 auto; min-height: 0; overflow-y: auto !important; }
	.actions { flex: none; display: flex; justify-content: flex-end; gap: 8px; }
	button { font-size: var(--text-caption); font-weight: var(--fw-semibold); cursor: pointer; }
	.cancel { padding: 5px 8px; border: none; background: none; color: var(--text-secondary); }
	.submit { padding: 6px 14px; border: none; border-radius: var(--radius-sm); background: var(--accent-fill); color: var(--accent-on-fill); }
	.submit:disabled { opacity: .4; cursor: default; }
	/* At touch widths each button is a full target, a target's width apart, so a miss on Cancel never posts. */
	@media (pointer: coarse), (max-width: 768px) { .actions { gap: var(--touch-target); } .actions button { min-width: var(--touch-target); min-height: var(--touch-target); } }
	.selection-reason { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
</style>
