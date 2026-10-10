<script lang="ts">
	let { name, color, size = 'sm', hidden = false }: { name: string; color: string; size?: 'sm' | 'md'; hidden?: boolean } = $props();
	const initials = $derived((name.trim()[0] ?? '?').toUpperCase());
</script>

<span class="avatar" class:md={size === 'md'} style="background:{color}" aria-label={hidden ? undefined : name} aria-hidden={hidden}>{initials}</span>

<style>
	.avatar { width: 19px; height: 19px; border-radius: var(--radius-circle); display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; font-size: var(--text-caption); font-weight: var(--fw-bold); color: #fff; }
	.avatar.md { width: 26px; height: 26px; font-size: var(--text-footnote); }
</style>
