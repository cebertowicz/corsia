import { NO_PICKS, type SetupPicks } from './components/SetupSubscriptionQuestion.svelte';

/** The two fields of `/api/setup/status` that say whether Claude Code or Codex is installed and signed in. */
export type InstallCheck = { ready_ai?: string | null; ai_check_pending?: boolean };

const PICKED_VENDORS = { claude: 'claude-code', chatgpt: 'codex' } as const;
/** How long Welcome waits for the install check before it asks anyway. */
const DETECT_CAP_MS = 2000, DETECT_POLL_MS = 400;

/**
 * What the person said they pay for on Welcome, and the install check the question waits behind.
 * Each checked tool is set up after the account, with no further click.
 */
export function createSetupPicks(deps: { status: () => InstallCheck; update: (next: InstallCheck) => void }) {
	let picks = $state<SetupPicks>({ ...NO_PICKS });
	/** CORSIA is still checking whether Claude Code or Codex is installed; the question waits behind a spinner. */
	let detecting = $state(false);
	/** Once the question has been shown it stays, even when a tool turns out to be ready a moment later. */
	let questionShown = $state(false);
	const any = $derived(picks.claude || picks.chatgpt || picks.key);
	const vendors = $derived<string[]>((['claude', 'chatgpt'] as const).filter((key) => picks[key]).map((key) => PICKED_VENDORS[key]));
	const waiting = () => Boolean(deps.status().ai_check_pending) && !deps.status().ready_ai;

	/** Hold the question back while the install check answers, for at most DETECT_CAP_MS; then it is shown and never collapses. */
	async function settle() {
		const until = Date.now() + DETECT_CAP_MS;
		detecting = waiting();
		while (detecting && Date.now() < until) {
			await new Promise((resolve) => setTimeout(resolve, DETECT_POLL_MS));
			try {
				const response = await fetch('/api/setup/status');
				if (response.ok) {
					const next = await response.json();
					deps.update({ ai_check_pending: next.ai_check_pending, ready_ai: next.ready_ai });
				}
			} catch { /* The cap ends the wait. */ }
			detecting = waiting();
		}
		detecting = false;
		questionShown = !deps.status().ready_ai;
	}

	return {
		get picks() { return picks; },
		get any() { return any; },
		get vendors() { return vendors; },
		get detecting() { return detecting; },
		get questionShown() { return questionShown; },
		settle,
		toggle(key: keyof SetupPicks) { picks = { ...picks, [key]: !picks[key] }; },
		/** Put back what the backend recorded on Welcome, after a reload. */
		restore(saved: SetupPicks) { picks = { ...saved }; },
	};
}
