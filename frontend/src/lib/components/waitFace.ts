import type { OperatorWait } from '$lib/api/plansTypes';
import { waitBack, waitChecksBack } from './plans/planWaits';

/** The chat's face of an outside wait (EW8), built once from the server's wait so every part reads the same value. */
export interface WaitFace {
	label: string;
	/** "back 11:46 PM", or "back now" once the time has passed (EW10); empty when the moment cannot be formatted. */
	back: string;
	/** How long it has waited, when the last turn's end is known; the face drops it otherwise. */
	elapsed: string | null;
	/** The sentence the title and accessible name carry. */
	title: string;
	/** The watched artifact the label opens, when there is one. */
	artifactId: string | null;
}

export function waitFace(wait: OperatorWait, elapsed: string | null, now: Date): WaitFace {
	const more = wait.more > 0 ? ` and ${wait.more} more` : '';
	const sofar = elapsed ? `, ${elapsed} so far` : '';
	const checks = waitChecksBack(wait.until, now);
	return {
		label: wait.label,
		back: waitBack(wait.until, now),
		elapsed,
		title: `Waiting for ${wait.label}${more}${sofar}.${checks ? ` The agent ${checks}.` : ''}`,
		artifactId: wait.artifact_id ?? null,
	};
}
