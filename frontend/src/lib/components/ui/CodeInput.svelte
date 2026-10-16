<script lang="ts">
	import { onMount, untrack } from 'svelte';

	/**
	 * A short code typed one character per box, drawn as the composer's chip. Only setup and the Devices row use it.
	 * Typing uppercases the character and moves on, a paste fills the boxes, Backspace on an empty box steps back, and
	 * the last character calls `onsubmit` by itself. `invalid` shows the boxes as rejected (the caller clears the value
	 * and shows its one line, then bumps `refocus`).
	 */
	interface Props {
		length?: number;
		value?: string;
		invalid?: boolean;
		/** Shows a code to read out, not to type: boxes of the same size and type, no caret and no focus ring (Settings › System). */
		readonly?: boolean;
		/** Boxes that take no input while the caller works (a code being checked). */
		disabled?: boolean;
		label: string;
		/** The box that has focus when it first shows; the first one when the code is empty or was just rejected. */
		focusAt?: number;
		/** Bump it to move focus to the first empty box again after the caller cleared a refused code. */
		refocus?: number;
		onsubmit?: (code: string) => void;
	}

	let { length = 4, value = $bindable(''), invalid = false, readonly = false, disabled = false, label, focusAt, refocus = 0, onsubmit }: Props = $props();

	let boxes: HTMLInputElement[] = $state([]);
	const chars = $derived(Array.from({ length }, (_, i) => value[i] ?? ''));

	onMount(() => { if (focusAt !== undefined) boxes[focusAt]?.focus({ preventScroll: true }); });

	let lastRefocus = untrack(() => refocus);
	$effect(() => {
		if (refocus === lastRefocus) return;
		lastRefocus = refocus;
		// preventScroll: a refusal line is scrolled into view by its owner, and focus must not push it back under the dock.
		boxes[Math.min(value.length, length - 1)]?.focus({ preventScroll: true });
	});

	function commit(next: string, focusAt?: number) {
		value = next.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, length);
		if (focusAt !== undefined) boxes[Math.min(focusAt, length - 1)]?.focus({ preventScroll: true });
		if (value.length === length) onsubmit?.(value);
	}

	function oninput(event: Event, i: number) {
		const typed = (event.currentTarget as HTMLInputElement).value;
		if (typed.length > 1) return commit(value.slice(0, i) + typed + value.slice(i + 1), i + typed.length);
		commit(value.slice(0, i) + typed + value.slice(i + 1), i + 1);
	}
	function onpaste(event: ClipboardEvent) {
		event.preventDefault();
		commit(event.clipboardData?.getData('text') ?? '', length);
	}
	function onkeydown(event: KeyboardEvent, i: number) {
		if (event.key === 'Backspace' && !chars[i] && i > 0) {
			event.preventDefault();
			commit(value.slice(0, i - 1), i - 1);
		}
	}
</script>

<div class="code" role="group" aria-label={label}>
	{#each chars as ch, i (i)}
		<input
			bind:this={boxes[i]}
			class="box" class:invalid class:filled={ch !== ''}
			type="text" inputmode="text" autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength={length}
			aria-label={`${label}, character ${i + 1} of ${length}`}
			value={ch}
			{readonly} {disabled}
			oninput={(e) => oninput(e, i)} onkeydown={(e) => onkeydown(e, i)} {onpaste}
		/>
	{/each}
</div>

<style>
	/* !important: the app sets every input to --text-input on a phone, which would shrink the code to body size. The code is read across a desk. */
	.code { display: flex; gap: var(--space-3); }
	.box {
		width: calc(var(--space-8) + var(--space-3)); height: calc(var(--space-8) + var(--space-4)); padding: 0;
		text-align: center; text-transform: uppercase; font-family: inherit;
		font-size: var(--text-title1) !important; font-weight: var(--fw-semibold); color: var(--text-primary);
		background: var(--glass-thin); border: 1px solid var(--border); border-radius: var(--radius-lg);
		box-shadow: var(--glass-rim), var(--shadow-chip); caret-color: var(--accent-line);
		transition: border-color var(--dur-tight);
	}
	.box:focus { outline: none; border-color: var(--accent-line); box-shadow: var(--glass-rim), var(--shadow-chip), 0 0 0 3px var(--accent-wash); }
	.box.invalid { border-color: var(--red-text); }
	.box:disabled { opacity: 1; cursor: default; }
	.box:read-only { caret-color: transparent; cursor: default; }
	.box:read-only:focus { border-color: var(--border); box-shadow: var(--glass-rim), var(--shadow-chip); }
	/* A typed character fades in; nothing pops. */
	.box.filled:not(:read-only) { animation: char-in var(--dur-tight) var(--ease-entrance); }
	@keyframes char-in { from { color: transparent; } }
	@media (prefers-reduced-motion: reduce) { .box.filled { animation: none; } }
</style>
