import { isMachineCode } from './plainError';

/**
 * Plain language for machine-readable delivery errors.
 *
 * Some delivery errors are already sentences a human wrote for a human ("The
 * selected model is no longer available"); those pass through. A snake_case
 * identifier is a code, and a code must never reach a user surface — being
 * shown `provider_write_outcome_unknown` and asked what to do about it is
 * asking the user a question they hold no evidence to answer.
 */
const BLOCKED_COPY: Readonly<Record<string, string>> = Object.freeze({
	missing_context_evidence: 'CORSIA could not rebuild this thread’s context',
	cancelled_by_user: 'you cancelled it',
	provider_unavailable: 'the provider was unreachable',
});

/**
 * Inputs CORSIA set aside itself. Saying "Cancelled by you" about them tells
 * the user they did something they did not.
 */
export const SET_ASIDE_COPY: Readonly<Record<string, {
	label: 'Held while paused' | 'Not needed';
	title: string;
}>> = Object.freeze({
	held_while_paused: {
		label: 'Held while paused',
		title: 'Arrived while the agent was paused. It is in the agent’s digest for its next Turn.',
	},
	stale_plan_wake: {
		label: 'Not needed',
		title: 'The work this was about moved on before the agent picked it up.',
	},
});

/** The hold cause the backend writes while a selected model cannot run. */
export const SELECTED_MODEL_UNAVAILABLE = 'selected_model_unavailable';

export const WAITING_FOR_MODEL = {
	label: 'Waiting for model',
	title: 'The selected model is unavailable. Your message is saved and will send when the model is back, or as soon as you switch models.',
} as const;

/** The same hold, written while the thread has no model at all. */
export const NO_MODEL_SELECTED = 'no_model_selected';

export const WAITING_FOR_MODEL_CHOICE = {
	label: 'Choose a model to send',
	title: 'This thread has no model yet. Your message is saved and will send as soon as you choose one.',
} as const;

/**
 * Errors that mean "we do not know", not "it failed".
 *
 * These get their own sentence rather than a `Not delivered:` prefix, because
 * the honest statement and the safe action are both different: the provider may
 * already hold this message, so sending again may send it twice.
 */
export const UNCONFIRMED_TITLE =
	'CORSIA couldn’t confirm the provider received this. Send it again if it '
	+ 'never arrived — it may arrive twice.';

export function blockedReason(value: unknown): string {
	if (typeof value !== 'string' || !value.trim()) {
		return 'CORSIA could not send it';
	}
	const reason = value.trim();
	if (reason in BLOCKED_COPY) return BLOCKED_COPY[reason];
	if (isMachineCode(reason)) return 'CORSIA could not send it';
	return reason;
}
