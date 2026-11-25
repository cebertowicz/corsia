<script lang="ts">
	import { keepTyped } from '$lib/actions/keepTyped';
	import { keepInView } from '$lib/actions/keepInView';
	import { autosize } from '$lib/actions/autosize';
	import Button from '$lib/components/ui/Button.svelte';
	import Field from '$lib/components/ui/Field.svelte';
	import MentionPicker from './MentionPicker.svelte';
	import { createMentionSelection } from './mentionSelection';
	import RecipientLine from './RecipientLine.svelte';
	import { recipientNames, type Recipient } from './recipients';
	import { showsDraft } from '$lib/stores/keptCommentDrafts.svelte';

	/** The reply field of a comment thread: a textarea that grows with its text, and a send button once it holds some. */
	let { value = $bindable(''), selection = null, keepVisible = false, wholeCard, posting = false, onSend, onSelection, goesTo = undefined, selectedTags = createMentionSelection(), shownKey = '' }: {
		/** The reply's text. The field grows with it and never scrolls itself; the card's outer scroller keeps the caret in view. */
		value?: string;
		selection?: [number, number] | null;
		keepVisible?: boolean;
		/** The card this field belongs to: kept whole in view whenever the scroller can hold it, so revealing the field never cuts its author and quote. */
		wholeCard?: HTMLElement;
		posting?: boolean;
		selectedTags?: ReturnType<typeof createMentionSelection>;
		onSend: () => void;
		/** The agents this reply wakes, from the server's reply rule; absent where the caller does not know (chat comments). */
		goesTo?: Recipient[];
		onSelection?: (selection: [number, number] | null) => void;
		/** The kept reply this field shows (`replyDraftKey`): a failed post says why under it, not in a toast, while it is on screen. */
		shownKey?: string;
	} = $props();

	const inputId = $props.id();
	let input = $state<HTMLTextAreaElement>();
	let picker = $state<MentionPicker>();
	/** The height of the line under the field (its own top gap included), 0 when there is none. Revealing the field leaves this much clear below it, so the person never sees Send without seeing who gets the reply, unless the field itself needs the room. */
	let lineHeight = $state(0);
	const gap = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--space-1')) || 4;
	/** Once the person taps the field or types, it stays in view in whatever scrolls the thread (a keyboard rising, the text
	 * growing). Focus alone doesn't: a card focused by picking its tag is already being scrolled to smoothly. */
	let typing = $state(false);
	export function mentions() { const offset = value.length - value.trimStart().length; return (picker?.occurrences(value) ?? []).map(item => ({ ...item, start: item.start - offset, end: item.end - offset })); }
	const tagged = $derived(picker && goesTo ? picker.occurrences(value).map((item) => ({ id: item.agent_id, name: value.slice(item.start, item.end).replace(/^@/, '') })) : []);
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

<div class="ct-compose open" data-reply-composer use:showsDraft={shownKey}>
	<MentionPicker bind:this={picker} {input} {selectedTags} />
	<Field variant="nested" hideLabel label="Reply" labelFor={inputId}>
		<textarea use:keepTyped id={inputId} class="ct-input" rows="1" placeholder="Reply…" readonly={posting} bind:this={input} use:keepInView={{ enabled: keepVisible || typing, whole: wholeCard, clearAfter: lineHeight ? lineHeight + gap() : undefined }} onpointerdown={() => (typing = true)} onblur={() => (typing = false)} bind:value use:autosize={value} oninput={() => { typing = true; saveSelection(); }} onselect={saveSelection} onkeyup={saveSelection} onpointerup={saveSelection} onkeydown={onKey} onclick={(event) => { event.stopPropagation(); saveSelection(); }}></textarea>
		{#if value.trim()}
			<span class="ct-send-slot">
				<Button variant="primary" size="sm" ariaLabel="Send reply" title="Reply" loading={posting} onclick={sendClicked}>
					{#if !posting}<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7" /></svg>{/if}
				</Button>
			</span>
		{/if}
	</Field>
	{#if goesTo}<div class="ct-goes" bind:offsetHeight={lineHeight}><RecipientLine names={recipientNames(goesTo, tagged)} none="No agent gets this reply." /></div>{/if}
</div>

<style>
	.ct-compose { margin-top: var(--space-1); }
	/* The field grows with its text and never scrolls itself. Revealing its end leaves a gap below it, so a scroll offset
	   rounded to a whole pixel never clips the Send that sits flush with its foot. */
	.ct-compose :global(.field .c textarea.ct-input) { max-height: none; scroll-margin-block-end: var(--space-1); }
	/* Holds the line's own top gap inside the box that is measured. */
	.ct-goes { display: flow-root; }
	/* Only where Send sits: at the field's foot, not squeezed. The button is the shared primitive, with no style of its own here. */
	.ct-send-slot { flex-shrink: 0; align-self: flex-end; display: flex; }
</style>
