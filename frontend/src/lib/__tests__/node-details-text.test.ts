import { describe, expect, it } from 'vitest';
import type { OperatorExecutionCause } from '$lib/api/plans';
import {
	costLine, nextMove, tryEnding, waitingTime, withFollowUps, workingTime,
} from '$lib/components/plans/nodeDetailsText';
import { node } from './fixtures/planTreeNode';

describe('node details wording', () => {
	it('names how a try ended, and never hides a reason it has no words for', () => {
		expect(tryEnding(null, null)).toBe('current');
		expect(tryEnding('later', 'failed')).toBe('released after failure');
		expect(tryEnding('later', 'spawned_thread_attached')).toBe('handed to a worker');
		expect(tryEnding('later', 'some_new_reason')).toBe('some new reason');
		expect(tryEnding('later', null)).toBe('ended');
	});

	it('shows measured lower bounds without calling subscription usage unpriced', () => {
		const amount = (
			known: number | null,
			unknownRows: number,
			knownTokens: number | null,
			tokenUnknownRows = 0,
			failures = 0,
		) => ({
			model_cost_usd: { known, unknown_rows: unknownRows },
			tokens: { known: knownTokens, unknown_rows: tokenUnknownRows },
			usage_recording_failures: failures,
			execution: {
				seconds: 0, unknown_turns: 0,
				turns: { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 },
			},
			waiting: { seconds: 0 },
		});
		expect(costLine(amount(null, 3, 10))).toBe('10 tokens');
		expect(costLine(amount(0.25, 1, 10))).toBe('at least $0.2500 API-equivalent · 10 tokens');
		expect(costLine(amount(1.5, 0, 10, 4, 2))).toBe('at least $1.500 API-equivalent · at least 10 tokens');
		expect(costLine(amount(0, 5, null))).toBeNull();
		expect(costLine(amount(null, 5, null))).toBeNull();
	});

	it('keeps working and waiting time out of the cost fact', () => {
		const turns = { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
		const amount = (waiting: number) => ({
			model_cost_usd: { known: 1.5, unknown_rows: 0 },
			tokens: { known: 10, unknown_rows: 0 },
			usage_recording_failures: 0,
			execution: { seconds: 7_800, unknown_turns: 0, turns },
			waiting: { seconds: waiting },
		});
		expect(costLine(amount(18_180))).toBe('$1.500 API-equivalent · 10 tokens');
		expect(workingTime(amount(18_180))).toBe('2h 10m');
		expect(waitingTime(amount(18_180))).toBe('5h 3m');
		expect(waitingTime(amount(0))).toBeNull();
	});

	it('names the next mover only when it is not an agent', () => {
		const cause = (owner_kind: OperatorExecutionCause['owner_kind']) =>
			({ owner_kind } as OperatorExecutionCause);
		expect(nextMove([cause('thread')])).toBeNull();
		expect(nextMove([cause('thread'), cause('user'), cause('user')])).toBe('You');
		expect(nextMove([cause('external'), cause('system')])).toBe('Outside CORSIA, CORSIA');
	});

	it('shows follow-up cost only when a known cost fact differs', () => {
		const commitment = (tokens: number, tries: number) => ({
			commitment_id: 'n',
			attempts: { total: tries, failed: 0, correction: 0, delegated: 0 },
			tokens: { known: tokens, unknown_rows: 0 },
			model_cost_usd: { known: null, unknown_rows: 0 },
			usage_recording_failures: 0,
			elapsed_seconds: { known: 0, status: 'in_progress' as const },
			user_effort: { known_plan_actions: 0, status: 'partial' as const },
		});
		const base = node({ id: 'n' });
		const rolled = { ...base.node_cost.rolled, tokens: { known: 100, unknown_rows: 0 } };
		const costed = { ...base, node_cost: { own: rolled, rolled } };
		expect(withFollowUps({ ...costed, commitment_cost: null })).toBeNull();
		expect(withFollowUps({ ...costed, commitment_cost: commitment(100, 0) })).toBeNull();
		expect(withFollowUps({ ...costed, commitment_cost: commitment(250, 3) })).toBe('250 tokens');
		expect(withFollowUps({
			...costed,
			commitment_cost: {
				...commitment(100, 3), usage_recording_failures: 1,
				tokens: { known: 100, unknown_rows: 2 },
				model_cost_usd: { known: null, unknown_rows: 1 },
			},
		})).toBe('at least 100 tokens');
	});
});
