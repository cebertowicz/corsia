<script lang="ts">
	import { formatMoment, localDayClock, parseUTC } from '$lib/time';
	/** `compact`: a list row's short form, with the zone and full date in the name and tooltip. */
	let { value, compact = false }: { value: string; compact?: boolean } = $props();
	const date = $derived(parseUTC(value));
	const valid = $derived(Number.isFinite(date.getTime()));
	const clock = (zone = false) => date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', ...(zone ? { timeZoneName: 'short' as const } : {}) });
	const sameDay = $derived(valid && date.toDateString() === $localDayClock.toDateString());
	const sameYear = $derived(valid && date.getFullYear() === $localDayClock.getFullYear());
	const label = $derived(!valid ? 'Time unavailable'
		: !compact ? formatMoment(date, $localDayClock, { zone: true })
		: sameDay ? clock()
		: `${date.toLocaleDateString([], { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }) })}, ${clock()}`);
	const full = $derived(valid && compact ? `${date.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })}, ${clock(true)}` : '');
</script>

<time datetime={valid ? value : undefined} title={full || undefined} aria-label={valid ? `${full || label} (${value})` : label}>{label}</time>

<style>
	time { display: block; color: var(--text-tertiary); font-weight: normal; overflow-wrap: anywhere; }
</style>
