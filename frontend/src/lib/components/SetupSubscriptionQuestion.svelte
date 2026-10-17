<script lang="ts" module>
	export type SetupPicks = { claude: boolean; chatgpt: boolean; key: boolean };
	export const NO_PICKS: SetupPicks = { claude: false, chatgpt: false, key: false };
</script>

<script lang="ts">
	import { RailRow, Spinner } from '$lib/components/ui';

	interface Props {
		picks: SetupPicks;
		onToggle: (key: keyof SetupPicks) => void;
		/** Claude Code or Codex is already ready: one line replaces the question. */
		ready?: string | null;
		/** CORSIA is still checking whether either is installed; the question waits. */
		detecting?: boolean;
	}
	const { picks, onToggle, ready = null, detecting = false }: Props = $props();
</script>

{#if ready}
	<p class="ready">
		<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
		<span>{ready} is ready. CORSIA uses it for chat.</span>
	</p>
{:else}
	<!-- While CORSIA checks, the question is laid out but hidden, so the spinner holds its place and nothing below moves when the question appears. -->
	<div class="stack">
	{#if detecting}<p class="detecting" role="status"><Spinner size={12} />Checking for Claude Code and Codex…</p>{/if}
	<div class="question" class:waiting={detecting} role="group" aria-labelledby="setup-question">
		<span class="ask" id="setup-question">Which of these do you pay for?</span>
		<span class="hint">Choose any that apply. CORSIA sets them up after you create your account.</span>
		<div class="picks">
			<RailRow pick="many" checked={picks.claude} label="Claude" meta="CORSIA installs Claude Code, made by Anthropic, and you sign in." onclick={() => onToggle('claude')} />
			<RailRow pick="many" checked={picks.chatgpt} label="ChatGPT" meta="CORSIA installs Codex, made by OpenAI, and you sign in." onclick={() => onToggle('chatgpt')} />
			<RailRow pick="many" checked={picks.key} label="A pay-as-you-go API key" meta="From Anthropic, OpenAI or another provider. You paste it after you create your account." onclick={() => onToggle('key')} />
		</div>
	</div>
	</div>
{/if}

<style>
	.stack { display: grid; }
	.stack > * { grid-area: 1 / 1; }
	.question { display: flex; flex-direction: column; gap: var(--space-1); transition: opacity var(--dur-standard) var(--ease-entrance); }
	.question.waiting { opacity: 0; visibility: hidden; transition: none; }
	.ask { color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-medium); }
	.hint { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	.picks { display: flex; flex-direction: column; gap: 1px; margin: var(--space-1) 0 0 calc(var(--space-2) * -1); }
	.ready { margin: 0; display: flex; align-items: center; gap: var(--space-2); color: var(--text-primary); font-size: var(--text-body); }
	.ready svg { flex: none; color: var(--accent-text); }
	.detecting { margin: 0; align-self: start; justify-self: start; display: inline-flex; align-items: center; gap: var(--space-2); color: var(--text-tertiary); font-size: var(--text-footnote); }
</style>
