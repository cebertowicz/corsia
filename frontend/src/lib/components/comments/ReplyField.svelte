<script lang="ts">
	import { keepInView } from '$lib/actions/keepInView';
	import { autosize } from '$lib/actions/autosize';
	import Field from '$lib/components/ui/Field.svelte';
	import MentionPicker from './MentionPicker.svelte';
	import { createMentionSelection } from './mentionSelection';

	/** The reply field of a comment thread: a textarea that grows with its text, and a send button once it holds some. */
	let { value = $bindable(''), selection = null, keepVisible = false, onSend, onSelection, selectedTags = createMentionSelection() }: {
		value?: string;
		selection?: [number, number] | null;
		keepVisible?: boolean;
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
</script>

<div class="ct-compose open">
	<MentionPicker bind:this={picker} {input} {selectedTags} />
	<Field variant="nested" hideLabel label="Reply" labelFor={inputId}>
		<textarea id={inputId} class="ct-input" rows="1" placeholder="Reply…" bind:this={input} use:keepInView={keepVisible} bind:value use:autosize={value} oninput={saveSelection} onselect={saveSelection} onkeyup={saveSelection} onpointerup={saveSelection} onkeydown={onKey} onclick={(event) => { event.stopPropagation(); saveSelection(); }}></textarea>
		{#if value.trim()}
			<button class="ct-send" onclick={(e) => { e.stopPropagation(); onSend(); }} aria-label="Send reply" title="Reply">
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
			</button>
		{/if}
	</Field>
</div>

<style>
	.ct-compose { margin-top: var(--space-1); }
	.ct-send { flex-shrink: 0; align-self: flex-end; display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: var(--radius-sm); background: var(--accent-fill); border: none; color: var(--accent-on-fill); cursor: pointer; transition: filter var(--dur-tight); }
	.ct-send:hover { filter: brightness(1.08); }
</style>
