import type {
	OperatorAttention,
	OperatorDisplay,
	OperatorLiveness,
	OperatorOwner,
	OperatorPlanLabelKey,
	OperatorPlanMeta,
	OperatorTarget,
} from './plansTypes';

export interface ActivityIndexRow {
	plan: OperatorPlanMeta;
	owner: OperatorOwner;
	progress: { open: number; liveness: OperatorLiveness };
	has_nodes: boolean;
	display: OperatorDisplay<OperatorPlanLabelKey>;
	priority_at: string;
}

export interface ActivityIndexEnvelope {
	contract_version: 1;
	representation: 'activity_scope_index';
	as_of: string;
	target: OperatorTarget;
	revision_token: string;
	plans: ActivityIndexRow[];
	attention: OperatorAttention;
}

export type ActivityIndexReadOutcome =
	| { status: 'fresh'; payload: ActivityIndexEnvelope; etag: string }
	| { status: 'not-modified' }
	| { status: 'not-found' }
	| { status: 'unauthorized' }
	| { status: 'forbidden' }
	// The same failure shape as OperatorReadOutcome: both come from `readPlan`.
	| { status: 'error'; message: string; reference?: string; reload?: true };
