<script lang="ts">
	import { Spinner } from '$lib/components/ui';
</script>

<section>
	<header>
		<h2>Spinner</h2>
		<p>The in-progress ring used by toasts, the update journey and Settings rows. Decorative; pair it with text that says what is happening.</p>
	</header>
	<div class="card">
		<Spinner />
		<Spinner size={12} />
		<Spinner size={18} />
	</div>
</section>

<style>
	section { margin-bottom: 32px; }
	header { margin-bottom: 12px; }
	h2 { font-size: var(--text-body); font-weight: 600; letter-spacing: -0.01em; color: var(--text-primary); margin: 0 0 4px; }
	p { font-size: var(--text-subhead); color: var(--text-tertiary); margin: 0; line-height: 1.5; }
	.card { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius); padding: 18px 22px; }
</style>
