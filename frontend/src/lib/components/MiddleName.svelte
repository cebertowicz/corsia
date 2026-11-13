<script lang="ts">
	// A long name keeps its last four characters (often the distinguishing suffix) and truncates in the middle, on one line at every width.
	let { name }: { name: string } = $props();
	const parts = $derived(name.length > 20 ? { head: name.slice(0, -4), tail: name.slice(-4) } : { head: name, tail: '' });
</script>

<span class="name" class:cut={!!parts.tail} title={name}><span class="name-head">{parts.head}</span>{#if parts.tail}<span class="name-tail">{parts.tail}</span>{/if}</span>

<style>
	/* A grid, so the cut name's floor is its min-content: a wrapper that keeps its automatic minimum never squeezes the name
	   below it (and a wrapper with min-width 0 still can). */
	.name { display: grid; grid-auto-flow: column; grid-template-columns: minmax(0, max-content); min-width: 0; white-space: nowrap; }
	.name-head { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	/* A name cut in the middle keeps its first letter and the ellipsis, so a short row never shows only the tail. */
	.name.cut { grid-template-columns: minmax(1.8em, max-content) max-content; }
</style>
