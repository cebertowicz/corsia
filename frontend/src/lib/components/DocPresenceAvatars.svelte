<script lang="ts">
	import type { PresenceView } from './DocPresenceStatus.svelte';
	import AvatarStack from './ui/AvatarStack.svelte';
	import { presenceMaximum } from './docPresenceFit';

	let { view, artifactId, instanceId, ready }: { view: PresenceView | null; artifactId: string | null; instanceId?: string; ready: boolean } = $props();
	let row = $state<HTMLElement | null>(null);
	let maximum = $state(3);

	// The title and presence share the remaining room. Measure each stack option without changing the visible row,
	// so a resize can both collapse and restore avatars without oscillating at the boundary.
	$effect(() => {
		const element = row;
		const participants = view?.participants;
		if (!element || !participants?.length || typeof ResizeObserver === 'undefined') return;
		const bar = element.closest<HTMLElement>('.shell-header');
		const title = bar?.querySelector<HTMLElement>('[data-testid="header-document-title"]');
		const titleCell = title?.parentElement;
		const tools = element.closest<HTMLElement>('.h-right');
		if (!bar || !title || !titleCell || !tools) { maximum = 3; return; }
		const canvas = document.createElement('canvas').getContext('2d');
		const fit = () => {
			const name = title.querySelector('.name')?.getAttribute('title') ?? '';
			if (!canvas || !name) return;
			const nameCell = title.querySelector<HTMLElement>('.name-size, .name') ?? title;
			canvas.font = getComputedStyle(nameCell).font;
			const shortened = name.length > 6 ? `${name.slice(0, 1)}…${name.slice(-5)}` : name;
			const needed = canvas.measureText(name).width + 18; // whole name plus the title's chevron and gap
			const floor = canvas.measureText(shortened).width + 18;
			const titleBox = title.getBoundingClientRect();
			const cellBox = titleCell.getBoundingClientRect();
			const toolsBox = tools.getBoundingClientRect();
			const overlap = Math.max(0, cellBox.right - toolsBox.left);
			const budget = titleBox.width + element.getBoundingClientRect().width - overlap;
			const width = (max: number) => element.querySelector<HTMLElement>(`[data-presence-measure="${max}"]`)?.getBoundingClientRect().width ?? Infinity;
			const single = element.querySelector<HTMLElement>('[data-presence-measure="1"]');
			const avatar = single?.querySelector<HTMLElement>('.participant-trigger, .participant-slot');
			const more = single?.querySelector<HTMLElement>('.more');
			const singleOverlaps = !!avatar && !!more && avatar.getBoundingClientRect().right > more.getBoundingClientRect().left;
			maximum = presenceMaximum(budget, width(3), width(1), needed, floor, singleOverlaps);
		};
		const observer = new ResizeObserver(fit);
		observer.observe(bar); observer.observe(titleCell); observer.observe(tools);
		fit();
		void document.fonts?.ready.then(fit);
		return () => observer.disconnect();
	});
</script>

{#if ready && artifactId && view?.artifactId === artifactId && view.instanceId === instanceId && view.participants.length}
	<span class="presence-row" aria-label="Recent document activity" bind:this={row}>
		<AvatarStack people={view.participants} max={maximum} separateMore />
		<span class="measures" aria-hidden="true" inert>
			{#each [3, 1, 0] as max}<span data-presence-measure={max}><AvatarStack people={view.participants} {max} measure separateMore /></span>{/each}
		</span>
	</span>
{/if}

<style>
	.presence-row { position: relative; }
	.measures { position: absolute; visibility: hidden; pointer-events: none; }
	.measures > span { position: absolute; white-space: nowrap; }
</style>
