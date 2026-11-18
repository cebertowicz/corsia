<script lang="ts">
	// A long name keeps its last four characters (often the distinguishing suffix) and truncates in the middle, on one line at every width.
	/**
	 * `tail`: the suffix the name keeps whenever it cuts, whatever its length (a document's extension). With it the cut is measured, so
	 * the ellipsis sits right against the kept end ("re…w.md"), and the name is capped at the cut's own width so what follows it (a ⌄)
	 * sits against it too. `refit`: changes whenever a neighbour gives room back without resizing anything the name watches.
	 */
	let { name, tail: keep, refit }: { name: string; tail?: string; refit?: unknown } = $props();
	const parts = $derived(name.length > 20 ? { head: name.slice(0, -4), tail: name.slice(-4) } : { head: name, tail: '' });
	const fitted = $derived(!!keep && name.endsWith(keep) && name.length > keep.length);

	let cell = $state<HTMLElement | null>(null);
	let root = $state<HTMLElement | null>(null);
	let shown = $state('');
	$effect(() => {
		const node = cell;
		const box = root;
		const full = name;
		void refit;
		if (!fitted || !keep || !node || !box || typeof ResizeObserver === 'undefined') return;
		// An extension alone after the ellipsis reads as four dots ("rev….md"): the cut keeps the name's last character before it.
		const end = full.length > keep.length + 1 ? full.slice(-(keep.length + 1)) : keep;
		const context = document.createElement('canvas').getContext('2d');
		const fit = () => {
			box.style.maxWidth = '';
			const width = node.getBoundingClientRect().width;
			if (!context || !width) { shown = full; return; }
			context.font = getComputedStyle(node).font;
			const wide = (text: string) => context.measureText(text).width;
			if (wide(full) <= width + 0.05) { shown = full; return; }
			const cut = (count: number) => `${full.slice(0, count).trimEnd()}…${end}`;
			let low = 1;
			let high = full.length - end.length - 1;
			// 1px to spare: a cut that overflows by a fraction of a pixel gets a second ellipsis from the browser.
			while (low < high) {
				const mid = Math.ceil((low + high) / 2);
				if (wide(cut(mid)) <= width - 1) low = mid; else high = mid - 1;
			}
			shown = cut(low);
			box.style.maxWidth = `${Math.ceil(wide(shown)) + 1}px`;
		};
		fit();
		const watcher = new ResizeObserver(fit);
		watcher.observe(node);
		// A neighbouring control can yield room without a window resize; watch the title button as well as the capped name.
		const button = box.closest('button');
		if (button) watcher.observe(button);
		// A capped name doesn't see its container widen, so a window resize refits too.
		window.addEventListener('resize', fit);
		void document.fonts?.ready.then(fit);
		return () => { watcher.disconnect(); window.removeEventListener('resize', fit); box.style.maxWidth = ''; };
	});
</script>

{#if fitted}
	<span class="name fitted" title={name} bind:this={root}><span class="name-size" aria-hidden="true" bind:this={cell}>{name}</span><span class="name-shown" aria-hidden="true" data-testid="middle-name-shown">{shown || name}</span><span class="name-read">{name}</span></span>
{:else}
	<span class="name" class:cut={!!parts.tail} title={name}><span class="name-head">{parts.head}</span>{#if parts.tail}<span class="name-tail">{parts.tail}</span>{/if}</span>
{/if}

<style>
	/* A grid, so the cut name's floor is its min-content: a wrapper that keeps its automatic minimum never squeezes the name
	   below it (and a wrapper with min-width 0 still can). */
	.name { display: grid; grid-auto-flow: column; grid-template-columns: minmax(0, max-content); min-width: 0; white-space: nowrap; }
	.name-head { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	/* A name cut in the middle keeps its first letter and the ellipsis, so a short row never shows only the tail. */
	.name.cut { grid-template-columns: minmax(1.8em, max-content) max-content; }
	/* The hidden whole name and the measured cut share one cell; the floor keeps a first letter, the ellipsis and the suffix. */
	.name.fitted { position: relative; grid-template-columns: minmax(min(4em, 100%), max-content); }
	.name-size, .name-shown { grid-area: 1 / 1; min-width: 0; overflow: hidden; white-space: nowrap; }
	.name-size { visibility: hidden; }
	.name-shown { text-overflow: ellipsis; }
	.name-read { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
</style>
