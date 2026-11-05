<script lang="ts" module>
	/** Which subscriptions the person pays for. */
	export type SetupPicks = { claude: boolean; chatgpt: boolean };
</script>

<script lang="ts">
	import { Button } from '$lib/components/ui';

	interface Props {
		/** The answer kept from an earlier visit, shown as the selected button. */
		answer?: SetupPicks | null;
		/** Whether this computer can run the free local model, which decides the no-subscription line. */
		eligible?: boolean;
		/** Whether the free model is already downloading or ready; the installer skips the download when a CLI is signed in. */
		localActive?: boolean;
		onAnswer: (picks: SetupPicks) => void;
	}
	const { answer = null, eligible = true, localActive = true, onAnswer }: Props = $props();
	const choices: { label: string; picks: SetupPicks }[] = [
		{ label: 'Claude', picks: { claude: true, chatgpt: false } },
		{ label: 'ChatGPT', picks: { claude: false, chatgpt: true } },
		{ label: 'Both', picks: { claude: true, chatgpt: true } },
		{ label: 'Neither', picks: { claude: false, chatgpt: false } },
	];
	const chosen = (picks: SetupPicks) => answer !== null && answer.claude === picks.claude && answer.chatgpt === picks.chatgpt;
	const fallback = $derived(!eligible ? 'No subscription? Add an API key on the next screen.'
		: localActive ? 'No subscription? The free model downloading to this computer works too, or add an API key.'
			: 'No subscription? Choose Neither and the free model downloads to this computer, or add an API key.');
</script>

<div class="question" role="group" aria-labelledby="setup-subscriptions">
	<span class="ask" id="setup-subscriptions">Which do you pay for?</span>
	<div class="choices">
		{#each choices as choice (choice.label)}
			<Button variant={chosen(choice.picks) ? 'primary' : 'secondary'} description={chosen(choice.picks) ? 'Your earlier answer' : undefined} onclick={() => onAnswer(choice.picks)}>{choice.label}</Button>
		{/each}
	</div>
	<p class="explain">Claude Code and Codex are the apps you’ll sign in to next.</p>
	<p class="explain">{fallback}</p>
</div>

<style>
	.question { display: flex; flex-direction: column; gap: var(--space-3); }
	.ask { color: var(--text-primary); font-size: var(--text-title3); font-weight: var(--fw-medium); }
	.explain { margin: 0; color: var(--text-secondary); font-size: var(--text-title3); line-height: var(--lh-normal); }
	.choices { display: flex; flex-wrap: wrap; gap: var(--space-2); }
</style>
