<script lang="ts">
	// A long name keeps its last four characters (often the distinguishing suffix) and truncates in the middle.
	let { name }: { name: string } = $props();
	const parts = $derived(name.length > 20 ? { head: name.slice(0, -4), tail: name.slice(-4) } : { head: name, tail: '' });
</script>

<span class="name" title={name}><span class="name-head">{parts.head}</span>{#if parts.tail}<span class="name-tail">{parts.tail}</span>{/if}</span>

<style>
	.name { display: flex; min-width: 0; white-space: nowrap; }
	.name-head { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.name-tail { flex: none; }
</style>
