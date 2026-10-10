import type { Model } from './models';
import type { ModelUsage } from './modelUsage';

/**
 * What one permitted account can do with a harness model now, as the backend decided it.
 * The entry marked `selected` is the one answer to which account this scope's next turn of
 * the model runs on; nothing here is derived again by a reader.
 */
export interface RunsOn {
	account_id: string;
	selected: boolean;
	status: 'ready' | 'available' | 'unavailable';
	status_reason: string | null;
	/** When a usage limit on this account clears for this model. */
	resets_at: string | null;
	/** The account's usage as it bears on this model: the plan windows that bind it. */
	usage: ModelUsage;
	/** On the selected entry: a person chose it during a running turn, and the next turn moves to it. */
	pending?: boolean;
}

/**
 * The selected account's decision for a model, whole: its status and reason, when a usage limit
 * on it clears, and the usage that binds the model. `accountTurn` returns it verbatim;
 * `modelAvailability` returns the same shape with the availability overlaid.
 */
export type AccountTurn = Pick<RunsOn, 'status' | 'status_reason' | 'resets_at'>
	& Partial<Pick<RunsOn, 'usage'>>;

/** What `modelAvailability` returns: `status` and `status_reason` say whether the model is available; `resets_at` and `usage` stay the selected account's. */
export type ModelAvailability = AccountTurn;

/**
 * Whether the model can run for this scope at all, as the server decided it across the
 * permitted accounts: ready when the selected account can run it, or when its only block is a
 * usage limit and another account can. The selected entry's own decision is untouched. The
 * backend sets only `ready` or `unavailable` on a harness row's `runnable`.
 */
export interface Runnable {
	status: 'ready' | 'unavailable';
	status_reason: string | null;
}

/**
 * What the account the next turn runs on decides for the model: the `runs_on` entry marked
 * selected, verbatim. A model with no such entry (local, API, or a scope that denies it) is its
 * own status. This is the account's fact (a held line, a limit), not whether the model is available.
 */
export function accountTurn(model: Model): AccountTurn {
	return model.runs_on?.find((entry) => entry.selected)
		?? { status: model.status, status_reason: model.status_reason ?? null, resets_at: null };
}

/**
 * Whether the model is available. Availability (status, reason) is the row's `runnable` when the
 * server sends one, else the selected account's; the reset and the account's usage always come
 * from the selected entry. A ready reading does not mean the next turn leaves now: with rollover
 * off the turn can still be held on a limited selected account (`accountTurn`).
 */
export function modelAvailability(model: Model): ModelAvailability {
	const selected = accountTurn(model);
	return model.runnable ? { ...selected, ...model.runnable } : selected;
}

/**
 * Whether a usage limit is the only thing between this model and a turn. The server holds such
 * an input and releases it in order when the limit clears or the thread moves.
 */
export function heldByUsageLimit(model: Model): boolean {
	const turn = modelAvailability(model);
	return turn.status === 'unavailable' && turn.status_reason === 'usage_limited';
}

/**
 * The one rule for whether a composer takes a typed message for a model. `runnable` decides
 * availability (`modelAvailability`); a model blocked only by a usage limit still accepts a held
 * send, which is a separate fact from it being available. Every other block (not offered, signed
 * out, model gone) does not. The chip and the send gate both read this.
 */
export function acceptsSend(model: Model): boolean {
	return modelAvailability(model).status === 'ready' || heldByUsageLimit(model);
}
