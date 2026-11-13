<script lang="ts">
	import { keepInView } from '$lib/actions/keepInView';
	import { autosize } from '$lib/actions/autosize';
	import Field from '$lib/components/ui/Field.svelte';
	import MentionPicker from './MentionPicker.svelte';
	import { createMentionSelection } from './mentionSelection';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	/** The reply field of a comment thread: a textarea that grows with its text, and a send button once it holds some. */
	let { value = $bindable(''), selection = null, keepVisible = false, wholeCard, posting = false, onSend, onSelection, selectedTags = createMentionSelection() }: {
		/** Replies grow until their available scroller is full, then scroll inside. */
		value?: string;
		selection?: [number, number] | null;
		keepVisible?: boolean;
		/** The card this field belongs to: kept whole in view whenever the scroller can hold it, so revealing the field never cuts its author and quote. */
		wholeCard?: HTMLElement;
		posting?: boolean;
		selectedTags?: ReturnType<typeof createMentionSelection>;
		onSend: () => void;
		onSelection?: (selection: [number, number] | null) => void;
	} = $props();

	const inputId = $props.id();
	let input = $state<HTMLTextAreaElement>();
	let picker = $state<MentionPicker>();
	export function mentions() { const offset = value.length - value.trimStart().length; return (picker?.occurrences(value) ?? []).map(item => ({ ...item, start: item.start - offset, end: item.end - offset })); }
	export function clearTags() { selectedTags.clear(); }
	let appliedSelection: string | null = null;
	export function focus(options?: FocusOptions) { input?.focus(options); }

	$effect(() => {
		if (!input || !selection) return;
		const next = selection.join(':');
		if (next === appliedSelection) return;
		input.setSelectionRange(...selection);
		appliedSelection = next;
	});

	function saveSelection() {
		if (!input) return;
		if (!value) {
			appliedSelection = null;
			onSelection?.(null);
			return;
		}
		appliedSelection = `${input.selectionStart}:${input.selectionEnd}`;
		onSelection?.([input.selectionStart, input.selectionEnd]);
	}

	// As in the chat composer: Enter sends and Shift+Enter starts a new line; on a touch screen Enter is a new line, and
	// Cmd or Ctrl+Enter sends everywhere.
	function onKey(e: KeyboardEvent) {
		if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.repeat) return;
		if (!(e.metaKey || e.ctrlKey) && window.matchMedia?.('(pointer: coarse)').matches) return;
		e.preventDefault();
		onSend();
	}
	// Send leaves with the text it sent, so focus goes back to the field, where Enter leaves it, rather than to the page;
	// on a touch screen that would raise the keyboard, so there the field stays as the tap left it.
	function sendClicked(e: MouseEvent) {
		e.stopPropagation();
		if (posting) return;
		if (!window.matchMedia?.('(pointer: coarse)').matches) input?.focus({ preventScroll: true });
		onSend();
	}
</script>

<div class="ct-compose open" data-autosize-sticky>
	<MentionPicker bind:this={picker} {input} {selectedTags} />
	<Field variant="nested" hideLabel label="Reply" labelFor={inputId}>
		<textarea id={inputId} class="ct-input" rows="1" placeholder="Reply…" readonly={posting} bind:this={input} use:keepInView={{ enabled: keepVisible, whole: wholeCard }} bind:value use:autosize={{ value, fit: true }} oninput={saveSelection} onselect={saveSelection} onkeyup={saveSelection} onpointerup={saveSelection} onkeydown={onKey} onclick={(event) => { event.stopPropagation(); saveSelection(); }}></textarea>
		{#if value.trim()}
			<button class="ct-send" data-send-reply aria-disabled={posting ? 'true' : undefined} aria-busy={posting} onclick={sendClicked} aria-label="Send reply" title="Reply">
				{#if posting}<Spinner size={14} />{:else}<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>{/if}
			</button>
		{/if}
	</Field>
</div>

<style>
	.ct-compose { margin-top: var(--space-1); }
	/* A thread popover or drawer scrolls its discussion behind the reply instead of hiding its field below the fold. */
	/* The pinned reply takes its surface's own fill: the popover card is elevated, the comment drawers are opaque secondary sheets. */
	:global([data-popover]) .ct-compose, :global([data-adaptive-drawer]) .ct-compose { --compose-fill: var(--bg-elevated); position: sticky; bottom: 0; z-index: 1; background: var(--compose-fill); }
	:global([data-adaptive-drawer]) .ct-compose { --compose-fill: var(--bg-secondary); }
	:global([data-popover]) .ct-compose::after, :global([data-adaptive-drawer]) .ct-compose::after { content: ''; position: absolute; inset: 100% 0 auto; height: var(--space-3); background: var(--compose-fill); pointer-events: none; }
	/* A card listed with others holds its field in its own flow: pinned, it would cover the card under it, and its fill and strip would poke out of the card's corners. */
	:global(.ct.stacked) .ct-compose { position: static; background: none; }
	:global(.ct.stacked) .ct-compose::after { display: none; }
	/* Replies grow until their available scroller is full, then scroll inside. */
	.ct-compose :global(.field .c textarea.ct-input) { max-height: none; }
	.ct-send { flex-shrink: 0; align-self: flex-end; display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: var(--radius-sm); background: var(--accent-fill); border: none; color: var(--accent-on-fill); cursor: pointer; transition: filter var(--dur-tight); }
	.ct-send:hover { filter: brightness(1.08); }
	/* In progress, not unavailable: the ring takes the send button's own ink, and the button keeps its fill. */
	.ct-send[aria-busy="true"] { --accent: var(--accent-on-fill); cursor: progress; filter: none; }
</style>
