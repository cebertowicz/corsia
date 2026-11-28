<script lang="ts">
	/**
	 * A device's name on one line. When it does not fit it keeps its start and its end and gives way in the middle; the
	 * join is the ellipsis and at most one space, so the gap after it never grows with the width left over. The full name
	 * is the title and the row's accessible name. The end is whole words, at least TAIL characters of them.
	 */
	let { name }: { name: string } = $props();

	const TAIL = 8;
	let box = $state<HTMLElement | undefined>();
	let width = $state(0);
	let font = $state('');
	const ctx = typeof document === 'undefined' ? null : document.createElement('canvas').getContext('2d');

	$effect(() => {
		if (!box) return;
		const cs = getComputedStyle(box);
		font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
	});

	function split(full: string): { head: string; tail: string; sep: string } {
		const words = full.split(' ');
		let end = words.pop() ?? '';
		while (end.length < TAIL && words.length > 1) end = `${words.pop()} ${end}`;
		if (end.length > full.length / 2) return { head: full.slice(0, -TAIL), tail: full.slice(-TAIL), sep: '' };
		return { head: words.join(' '), tail: end, sep: ' ' };
	}

	const shown = $derived.by(() => {
		if (!ctx || !width || !font) return name;
		ctx.font = font;
		const fits = (text: string) => ctx.measureText(text).width <= width;
		if (fits(name)) return name;
		const { head, tail, sep } = split(name);
		const join = (n: number) => `${head.slice(0, n).trimEnd()}…${sep}${tail}`;
		let low = 1;
		let high = head.length;
		while (low < high) {
			const mid = Math.ceil((low + high) / 2);
			if (fits(join(mid))) low = mid; else high = mid - 1;
		}
		return join(low);
	});
</script>

<span class="mid" title={name} bind:this={box} bind:clientWidth={width}>{shown}</span>

<style>
	.mid { display: block; min-width: 0; overflow: hidden; white-space: pre; }
</style>
