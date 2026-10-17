import type { SetupPicks } from './components/SetupSubscriptionQuestion.svelte';

/** First-run setup progress, which the backend owns: the app reads it to resume and reports changes to it. */
export type SetupProgress = {
	setup_completed: boolean;
	/** Where to reopen an unfinished setup: Connect an AI, or the oMLX question inside it. */
	step: 'models' | 'local-consent' | null;
	local_ai: boolean | null;
	/** What was checked on Welcome, or null when nothing was recorded. */
	picks?: SetupPicks | null;
	/** Eligibility facts, read fresh by the backend each time; present only while setup is unfinished. */
	status?: Record<string, unknown>;
};

export type SetupProgressChange = { step?: 'models' | 'local-consent'; local_ai?: boolean; picks?: SetupPicks; completed?: boolean };

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
 * choice and Welcome's picks are taken, with no session. Returns whether it was saved: a failed step or choice write is not
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
