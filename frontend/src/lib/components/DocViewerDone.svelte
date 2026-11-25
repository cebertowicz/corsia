<script lang="ts">
	// Touch Edit's way out, in the document header where the avatars sit. It never takes focus from the document, so the keyboard stays up.
	let { onDone }: { onDone?: () => void } = $props();
</script>

<button type="button" class="bar-done" onpointerdown={(event) => event.preventDefault()} onmousedown={(event) => event.preventDefault()} onclick={() => onDone?.()}>Done</button>

<style>
	.bar-done { flex: none; display: inline-flex; align-items: center; justify-content: center; width: var(--touch-target); min-width: var(--touch-target); height: var(--touch-target); min-height: var(--touch-target); padding: 0 var(--space-1); border: 0; border-radius: var(--radius-sm); background: none; color: var(--accent-text); font: inherit; font-size: var(--text-footnote); font-weight: var(--fw-semibold); white-space: nowrap; cursor: pointer; }
	.bar-done:hover { background: var(--bg-hover); }
	.bar-done:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
