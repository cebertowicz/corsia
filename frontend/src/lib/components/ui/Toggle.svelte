<script lang="ts">
	interface Props {
		checked: boolean;
		onchange: (next: boolean) => void;
		ariaLabel?: string;
		/** The id of the text that says why the switch is locked or what it does; read after its name. */
		describedBy?: string;
		disabled?: boolean;
		/** The choice holds and cannot change right now (a start in progress): full colour, no dimming, no tap. */
		locked?: boolean;
		/** A rule set elsewhere holds the choice: muted and inert, while still focusable so its description remains reachable. */
		held?: boolean;
		size?: 'sm' | 'md';
	}

	const { checked, onchange, ariaLabel, describedBy, disabled = false, locked = false, held = false, size = 'md' }: Props = $props();

	function handle() {
		if (disabled || locked || held) return;
		onchange(!checked);
	}
</script>

<button
	type="button"
	class="tg {size}"
	class:on={checked}
	class:locked
	class:held
	aria-disabled={held || locked || undefined}
	{disabled}
	aria-pressed={checked}
	aria-label={ariaLabel}
	aria-describedby={describedBy}
	onclick={handle}
></button>

<style>
	.tg {
		--w: 30px;
		--h: 17px;
		--knob: 12px;
		width: var(--w); height: var(--h);
		background: var(--bg-tertiary);
		/* An off switch reads as a control, not a faint field: a darker outline. */
		border: 1px solid var(--text-tertiary);
		border-radius: var(--radius-pill);
		position: relative;
		cursor: pointer;
		padding: 0;
		transition: all var(--dur-tight) var(--ease-snap);
	}
	.tg.md {
		--w: 34px;
		--h: 19px;
		--knob: 14px;
	}
	.tg::after {
		content: '';
		position: absolute;
		width: var(--knob); height: var(--knob);
		border-radius: var(--radius-circle);
		background: linear-gradient(180deg, #d8d8e0, #b8b8c0);
		top: 50%; left: 1.5px;
		transform: translateY(-50%);
		transition: all var(--dur-tight) var(--ease-snap);
	}
	.tg.on {
		background: linear-gradient(180deg, var(--accent-hover, var(--accent)), var(--accent));
		border-color: var(--accent);
	}
	.tg.on::after {
		left: calc(var(--w) - var(--knob) - 2.5px);
		background: linear-gradient(180deg, #ffffff, #f0f0f5);
	}
	.tg:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.tg.locked { cursor: default; }
	.tg.held { opacity: 0.5; cursor: default; }
	.tg:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
</style>
