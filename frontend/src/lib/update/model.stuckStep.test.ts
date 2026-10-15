import { describe, expect, it } from 'vitest';
import { stepIndex, type UpdateOperation } from './model';

const op = (patch: Partial<UpdateOperation>) =>
	({ operation_id: 'u', kind: 'update', status: 'in_flight', phase: 'holding', ...patch }) as UpdateOperation;

// Root cause of "stuck on Getting the update": stepIndex() falls back to step 0 for every operation it
// cannot place, so a client that missed the fast phases (or reads a settled row, phase NULL) shows step 0.
describe('stepIndex never answers step 0 for an operation that is past it', () => {
	it('does not report step 0 for a complete operation (phase is cleared on settle)', () => {
		expect(stepIndex(op({ status: 'complete', phase: null }), false)).not.toBe(0);
	});

	it('does not report step 0 for a failed operation', () => {
		expect(stepIndex(op({ status: 'failed', phase: null }), false)).not.toBe(0);
	});

	it('reports Restarting when the server is unreachable and the last phase seen was holding', () => {
		expect(stepIndex(op({ phase: 'holding' }), true)).toBe(2);
	});
});
