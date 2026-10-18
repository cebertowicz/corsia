<script lang="ts">
	import { Field } from '$lib/components/ui';
	import { autosize } from '$lib/actions/autosize';

	/**
	 * One text field in two places: straight on a pane (`chip`, glass fill) and inside a card or sheet (`nested`, plain
	 * outline). Each shows input and textarea in rest, focus (click into it), filled, error and disabled.
	 */
	const longText = 'A multi-line field grows from one line to about six, then scrolls. This one is long enough to wrap onto a few lines at the width of a phone.';
	const states = [
		{ id: 'rest', label: 'Rest', value: '', error: undefined, disabled: false },
		{ id: 'focus', label: 'Focus: click into it', value: '', error: undefined, disabled: false },
		{ id: 'filled', label: 'Filled', value: 'Steve Cebertowicz', error: undefined, disabled: false },
		{ id: 'error', label: 'Error', value: 'short', error: 'Use at least 8 characters.', disabled: false },
		{ id: 'disabled', label: 'Disabled', value: 'Not editable', error: undefined, disabled: true },
	] as const;
</script>

<section class="block" id="text-field">
	<header class="block-head">
		<h2>Text field</h2>
		<p>One control for free text: 1px border, large radius, 16px type. On a pane it takes the chip's glass fill; inside a card or sheet it is a plain outline. A multi-line field grows to about six lines, then scrolls.</p>
	</header>
	<div class="card">
		<h3>On a pane: chip</h3>
		<div class="grid onpane">
			{#each states as state (state.id)}
				<Field variant="chip" layout="stacked" label={state.label} labelFor={`tf-pane-input-${state.id}`} error={state.error}>
					<input id={`tf-pane-input-${state.id}`} type="text" placeholder="Type here" value={state.value} disabled={state.disabled} aria-invalid={state.error ? 'true' : undefined} />
				</Field>
				<Field variant="chip" layout="stacked" label={`${state.label}, multi-line`} labelFor={`tf-pane-area-${state.id}`} error={state.error}>
					<textarea use:autosize id={`tf-pane-area-${state.id}`} rows="1" placeholder="Type here" value={state.id === 'filled' ? longText : state.value} disabled={state.disabled} aria-invalid={state.error ? 'true' : undefined}></textarea>
				</Field>
			{/each}
		</div>
		<h3>Inside a card or sheet: nested</h3>
		<div class="nested-card">
			<div class="grid">
				{#each states as state (state.id)}
					<Field variant="nested" layout="stacked" label={state.label} labelFor={`tf-card-input-${state.id}`} error={state.error}>
						<input id={`tf-card-input-${state.id}`} type="text" placeholder="Type here" value={state.value} disabled={state.disabled} aria-invalid={state.error ? 'true' : undefined} />
					</Field>
					<Field variant="nested" layout="stacked" label={`${state.label}, multi-line`} labelFor={`tf-card-area-${state.id}`} error={state.error}>
						<textarea use:autosize id={`tf-card-area-${state.id}`} rows="1" placeholder="Type here" value={state.id === 'filled' ? longText : state.value} disabled={state.disabled} aria-invalid={state.error ? 'true' : undefined}></textarea>
					</Field>
				{/each}
			</div>
		</div>
	</div>
</section>

<style>
	.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr)); gap: var(--space-4); align-items: start; }
	.nested-card { padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--glass-thin); box-shadow: var(--glass-rim); }
</style>
