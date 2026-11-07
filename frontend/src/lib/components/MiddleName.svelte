<script lang="ts">
	// A long name keeps its last four characters (often the distinguishing suffix) and truncates in the middle.
	// `full` (an open thread, where a touch screen has no hover to read the title) shows the whole name and wraps it.
	let { name, full = false }: { name: string; full?: boolean } = $props();
	const parts = $derived(name.length > 20 && !full ? { head: name.slice(0, -4), tail: name.slice(-4) } : { head: name, tail: '' });
</script>

<span class="name" class:full title={name}><span class="name-head">{parts.head}</span>{#if parts.tail}<span class="name-tail">{parts.tail}</span>{/if}</span>

<style>
	.name { display: flex; min-width: 0; white-space: nowrap; }
	.name-head { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.name-tail { flex: none; }
	.name.full { display: block; flex: 1 1 auto; white-space: normal; overflow-wrap: anywhere; }
</style>
