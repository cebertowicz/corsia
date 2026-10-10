/**
 * Operator plan cost wire types -- the commitment total and per-node roll-up
 * published by `backend/plans/commitment_cost.py` and `backend/plans/node_cost.py`.
 */

export interface OperatorCommitmentCost {
	commitment_id: string;
	attempts: {
		total: number;
		failed: number;
		correction: number;
		delegated: number;
	};
	tokens: { known: number | null; unknown_rows: number };
	model_cost_usd: { known: number | null; unknown_rows: number };
	usage_recording_failures: number;
	elapsed_seconds: { known: number; status: 'in_progress' | 'complete' };
	user_effort: { known_plan_actions: number; status: 'partial' };
}

/** One node's usage/execution facts, before roll-up: its own calls and Turns alone. */
export interface OperatorCostAmount {
	tokens: { known: number | null; unknown_rows: number };
	model_cost_usd: { known: number | null; unknown_rows: number };
	usage_recording_failures: number;
	execution: {
		seconds: number;
		unknown_turns: number;
		turns: { completed: number; error: number; cancelled: number; running: number; waiting_retry: number };
	};
	/** Wall clock spent in an agent, external, dependency or PR-review wait; never spend. */
	waiting: { seconds: number };
}

/** A node's own figure, and its own plus every descendant's (`rolled`). */
export interface OperatorNodeCost {
	own: OperatorCostAmount;
	rolled: OperatorCostAmount;
}

/**
 * One cost across a follow-up lineage (`plans/node_cost_initiative.py`), the
 * same value on every member. `follow_ups` is the members minus the root;
 * `cost` covers everything beneath them. `open` is true while any member is
 * unfinished.
 */
export interface OperatorInitiative {
	follow_ups: number;
	cost: OperatorCostAmount;
	open: boolean;
}
