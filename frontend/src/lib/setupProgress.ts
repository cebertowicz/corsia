import type { SetupAttempt } from './setupConnect.svelte';
export type SetupPicks = { claude: boolean; chatgpt: boolean };
export type SetupHandoff = { tools: Record<string, SetupAttempt> };

/** First-run setup progress, which the backend owns: the app reads the eligibility facts and reports changes to it. */
export type SetupProgress = {
	setup_completed: boolean;
	/** Where an unfinished setup stands: Connect an AI, or the oMLX question inside it. */
	step: 'models' | 'local-consent' | null;
	local_ai: boolean | null;
	/** What they said they pay for (Claude, ChatGPT), or null when nothing was recorded. */
	picks?: SetupPicks | null;
	/** Eligibility facts, read fresh by the backend each time; present only while setup is unfinished. */
	status?: Record<string, unknown>;
};

export type SetupProgressChange = { step?: 'models' | 'local-consent'; local_ai?: boolean; picks?: SetupPicks; completed?: boolean; handoff?: SetupHandoff };

const PROGRESS = '/api/setup/progress';

/** The signed-in person's setup progress, or null when it cannot be read. */
export async function fetchSetupProgress(sessionToken: string | null): Promise<SetupProgress | null> {
	if (!sessionToken) return null;
	try {
		const response = await fetch(PROGRESS, { headers: { Authorization: `Bearer ${sessionToken}` } });
		if (!response.ok) return null;
		const progress = await response.json();
		return typeof progress?.setup_completed === 'boolean' ? progress as SetupProgress : null;
	} catch {
		return null;
	}
}

/**
 * Tell the backend what the person chose or finished. Before the account exists only the local-AI
 * choice and the subscriptions they pay for are taken, with no session. Returns whether it was saved: a failed step or choice write is not
 * shown (the next change records it), but completion has no next change, so its caller must act on false.
 */
export async function recordSetupProgress(change: SetupProgressChange, sessionToken: string | null): Promise<boolean> {
	try {
		const response = await fetch(PROGRESS, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json', ...(sessionToken ? { Authorization: `Bearer ${sessionToken}` } : {}) },
			body: JSON.stringify(change),
		});
		return response.ok;
	} catch {
		return false;
	}
}

/** What starting an unfinished app-made setup over did: nothing (a guard refused) or a full reset. */
export type SetupReset = { reset: boolean; refused: string | null };

/**
 * Ask the backend to start the signed-in person's unfinished setup over. It resets only an account the app's own setup
 * made that nobody has used yet; any other install answers `reset: false` and is left exactly as it was. Null when unreadable.
 */
export async function resetUnfinishedSetup(sessionToken: string | null): Promise<SetupReset | null> {
	if (!sessionToken) return null;
	try {
		const response = await fetch('/api/setup/reset-unfinished', { method: 'POST', headers: { Authorization: `Bearer ${sessionToken}` } });
		if (!response.ok) return null;
		const result = await response.json();
		return typeof result?.reset === 'boolean' ? result as SetupReset : null;
	} catch {
		return null;
	}
}
