<script lang="ts" module>
	/** Which subscriptions the person pays for; a signed-in tool counts as paid for. */
	export type SetupPicks = { claude: boolean; chatgpt: boolean };

	/** The question screen 3 asks, or null when both tools are signed in and it is skipped. */
	export function subscriptionQuestion(claudeReady: boolean, codexReady: boolean): 'both' | 'claude' | 'chatgpt' | null {
		if (claudeReady && codexReady) return null;
		if (claudeReady) return 'chatgpt';
		if (codexReady) return 'claude';
		return 'both';
	}
</script>

<script lang="ts">
	import { Button } from '$lib/components/ui';

	interface Props {
		claudeReady: boolean;
		codexReady: boolean;
		/** The answer kept from an earlier visit, shown as the selected button. */
		answer?: SetupPicks | null;
		onAnswer: (picks: SetupPicks) => void;
	}
	const { claudeReady, codexReady, answer = null, onAnswer }: Props = $props();
	const question = $derived(subscriptionQuestion(claudeReady, codexReady));
	const choices = $derived<{ label: string; picks: SetupPicks }[]>(question === 'both' ? [
		{ label: 'Claude', picks: { claude: true, chatgpt: false } },
		{ label: 'ChatGPT', picks: { claude: false, chatgpt: true } },
		{ label: 'Both', picks: { claude: true, chatgpt: true } },
		{ label: 'Neither', picks: { claude: false, chatgpt: false } },
	] : question === 'chatgpt' ? [
		{ label: 'Yes', picks: { claude: true, chatgpt: true } },
		{ label: 'No', picks: { claude: true, chatgpt: false } },
	] : [
		{ label: 'Yes', picks: { claude: true, chatgpt: true } },
		{ label: 'No', picks: { claude: false, chatgpt: true } },
	]);
	const chosen = (picks: SetupPicks) => answer !== null && answer.claude === picks.claude && answer.chatgpt === picks.chatgpt;
</script>

{#if question}
	{#if question !== 'both'}
		<p class="ready">
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
			<span>{question === 'chatgpt' ? 'Claude Code' : 'Codex'} is signed in.</span>
		</p>
	{/if}
	<div class="question" role="group" aria-labelledby="setup-subscriptions">
		<span class="ask" id="setup-subscriptions">{question === 'both' ? 'Do you pay for Claude or ChatGPT?' : `Do you also pay for ${question === 'chatgpt' ? 'ChatGPT' : 'Claude'}?`}</span>
		<div class="choices">
			{#each choices as choice (choice.label)}
				<Button variant={chosen(choice.picks) ? 'primary' : 'secondary'} description={chosen(choice.picks) ? 'Your earlier answer' : undefined} onclick={() => onAnswer(choice.picks)}>{choice.label}</Button>
			{/each}
		</div>
	</div>
{/if}

<style>
	.question { display: flex; flex-direction: column; gap: var(--space-3); }
	.ask { color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-medium); }
	.choices { display: flex; flex-wrap: wrap; gap: var(--space-2); }
	.ready { margin: 0 0 var(--space-4); display: flex; align-items: center; gap: var(--space-2); color: var(--text-primary); font-size: var(--text-body); }
	.ready svg { flex: none; color: var(--accent-text); }
</style>
