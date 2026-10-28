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
		title: 'The task this message was for moved on before the agent picked it up.',
	},
});

/** The hold cause the backend writes while a selected model cannot run. */
export const SELECTED_MODEL_UNAVAILABLE = 'selected_model_unavailable';

export const WAITING_FOR_MODEL = {
	label: 'Waiting for model',
	title: 'The selected model is unavailable. Your message is saved and will send when the model is back, or as soon as you switch models.',
} as const;

/** The local-only project policy held a message still bound to a cloud model. */
export const PROJECT_CLOUD_OFF = 'project_cloud_off';

/** The action that resumes either locality hold. */
export const SWITCH_TO_LOCAL_MODEL = 'Switch to a local model to send';

export const WAITING_FOR_LOCAL_MODEL = {
	label: SWITCH_TO_LOCAL_MODEL,
	title: 'Local models only is on for this project. Your message is saved and will send as soon as you switch this chat to a local model or turn Local models only off.',
} as const;

/** The chat itself excludes cloud models, so only switching this chat can resume it. */
export const THREAD_LOCAL_ONLY = 'thread_local_only';

export const WAITING_FOR_THREAD_LOCAL_MODEL = {
	label: SWITCH_TO_LOCAL_MODEL,
	title: 'Local only is on for this chat. Your message is saved and will send as soon as you switch this chat to a local model.',
} as const;

/** The same hold, written while the thread has no model at all. */
export const NO_MODEL_SELECTED = 'no_model_selected';

export const WAITING_FOR_MODEL_CHOICE = {
	label: 'Choose a model to send',
	title: 'This thread has no model yet. Your message is saved and will send as soon as you choose one.',
} as const;

export const CREATE_ACCOUNT_REFUSED = 'Couldn’t start the chat on that account. The model menu shows why; pick another account or fix it in Settings › Providers.';
export const WAITING_FOR_ACCOUNT = 'Waiting for account';

const SIGN_IN_AGAIN = 'Your message is saved and will send as soon as you sign in again in Settings › Providers.';
const SWITCH_MODELS = 'Your message is saved and will send as soon as you switch models';
const SIGN_IN_MISSING = `CORSIA can’t find this chat’s account sign-in on this computer. ${SIGN_IN_AGAIN}`;

/**
 * Account holds by cause, keyed by `connections.account_holds.ACCOUNT_HOLD_REASONS`.
 *
 * A removed account stays removed: adding it again creates a new account that
 * the held chat is not bound to, so switching models is its only route.
 */
export const ACCOUNT_HOLD_COPY: Readonly<Record<string, { label: typeof WAITING_FOR_ACCOUNT; title: string }>> = Object.freeze({
	account_signed_out: { label: WAITING_FOR_ACCOUNT, title: `This chat’s account is signed out. ${SIGN_IN_AGAIN}` },
	account_login_unverified: {
		label: WAITING_FOR_ACCOUNT,
		title: `CORSIA couldn’t confirm this chat’s account is still signed in. ${SIGN_IN_AGAIN}`,
	},
	account_home_mismatch: { label: WAITING_FOR_ACCOUNT, title: SIGN_IN_MISSING },
	codex_auth_source_missing: { label: WAITING_FOR_ACCOUNT, title: SIGN_IN_MISSING },
	codex_auth_projection_changed: { label: WAITING_FOR_ACCOUNT, title: SIGN_IN_MISSING },
	account_model_not_discovered: {
		label: WAITING_FOR_ACCOUNT,
		title: `CORSIA couldn’t find this model on this chat’s account. ${SWITCH_MODELS}, or sign in again in Settings › Providers.`,
	},
	account_removed: { label: WAITING_FOR_ACCOUNT, title: `This chat’s account isn’t set up in CORSIA. ${SWITCH_MODELS}.` },
	// The denying policy may be the workspace's or the project's and the hold
	// does not say which, so no settings pointer: it could name a place with no toggle.
	account_project_denied: {
		label: WAITING_FOR_ACCOUNT,
		title: `Your provider settings don’t allow this chat’s account. ${SWITCH_MODELS}.`,
	},
	project_policy_missing: {
		label: WAITING_FOR_ACCOUNT,
		title: `This project doesn’t allow this chat’s account. ${SWITCH_MODELS}.`,
	},
	account_runtime_pending: {
		label: WAITING_FOR_ACCOUNT,
		title: 'This chat’s account isn’t ready to run. Your message is saved and will send when it is.',
	},
});

/** Any hold code this client has no copy for yet: true, and never a wrong cause. */
export const WAITING_TO_SEND = {
	label: 'Waiting to send',
	title: 'Your message is saved and will send when CORSIA can deliver it.',
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
