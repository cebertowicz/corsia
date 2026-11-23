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
		const bar = element.closest<HTMLElement>('[data-document-bar-row]');
		const title = bar?.querySelector<HTMLElement>('[data-document-title]');
		const titleCell = title?.parentElement;
		const tools = element.closest<HTMLElement>('[data-document-bar-tools]');
		const full = title?.querySelector<HTMLElement>('[data-name-full]');
		const minimum = title?.querySelector<HTMLElement>('[data-name-minimum]');
		const chevron = title?.querySelector<SVGElement>('svg');
		if (!bar || !title || !titleCell || !tools || !full || !minimum || !chevron) { maximum = 3; return; }
		const fit = () => {
			const fullName = full.getBoundingClientRect().width;
			const minimumName = Math.min(fullName, minimum.getBoundingClientRect().width);
			const spacing = chevron.getBoundingClientRect().width + (parseFloat(getComputedStyle(title).columnGap) || 0);
			const needed = fullName + spacing;
			const floor = minimumName + spacing;
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
			maximum = presenceMaximum({ budget, fullStackWidth: width(3), singleWidth: width(1), countWidth: width(0), fullTitleWidth: needed, floorTitleWidth: floor, singleOverlaps });
		};
		const observer = new ResizeObserver(fit);
		observer.observe(bar); observer.observe(titleCell); observer.observe(tools); observer.observe(full); observer.observe(minimum);
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
