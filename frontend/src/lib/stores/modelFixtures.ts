import type { Model } from './models';
import type { RunsOn } from './modelTurn';
import type { ModelUsage } from './modelUsage';

/** What the selected account decides for a model's next turn, as a test states it. */
export interface TurnFields {
	status?: Model['status'];
	status_reason?: string;
	status_resets_at?: string | null;
	usage?: ModelUsage;
}

const TURN_KEYS = ['status_resets_at', 'usage'] as const;

/**
 * A model row in the shape `/api/models` returns it: the model's own status, plus one selected
 * `runs_on` entry holding the account's decision whenever the test names one (usage, a reset, a
 * recheck). A row with no account fields keeps its status as its own, as a local model's does.
 */
export function withAccountTurn(row: Model & TurnFields): Model {
	const { status_resets_at, usage, ...model } = row;
	if (!TURN_KEYS.some((key) => row[key] !== undefined)) return model as Model;
	const entry: RunsOn = {
		account_id: 'acct-1',
		selected: true,
		status: row.status ?? 'ready',
		status_reason: row.status_reason ?? null,
		resets_at: status_resets_at ?? usage?.resets_at ?? null,
		usage: usage ?? { state: 'unknown', usable: true, windows: [] },
	};
	return { ...model, status: 'ready', status_reason: undefined, runs_on: [entry] };
}
