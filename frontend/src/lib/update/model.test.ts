import { describe, expect, it } from 'vitest';
import { failureCopy, formatScheduleTime, holdsNewWork, stepDetail, type UpdateFailure, type UpdateOperation } from './model';

const failure = (code: UpdateFailure['code']): UpdateFailure => ({
	code, phase: null, detail: null, summary: 'Stored failure summary.', conductorTold: false, log: null,
});

describe('update model', () => {
	it('formats every configured schedule time in 12-hour time', () => {
		expect(formatScheduleTime('03:00')).toBe('3:00 AM');
		expect(formatScheduleTime('15:05')).toBe('3:05 PM');
		expect(formatScheduleTime('00:00')).toBe('12:00 AM');
	});

	it('words the safe-point count for one agent in the singular', () => {
		const op = { operation_id: 'u', kind: 'update', status: 'in_flight', phase: 'draining', drain_total: 1, blocking: 1, hard_cap_remaining: 299 } as UpdateOperation;
		expect(stepDetail(op, false)).toBe('0 of 1 agent is at a safe point. Stopping the rest in 4:59.');
		expect(stepDetail({ ...op, drain_total: 3, blocking: 3 } as UpdateOperation, false)).toContain('0 of 3 agents are at a safe point.');
	});

	it.each([
		['disk_space', 'Not enough disk space for a safety backup.'],
		['fetch_failed', "Couldn't download the update."],
		['build_failed', "Couldn't build the update."],
		['launch_failed', "Couldn't start the restart."],
		['drain_cancelled', 'The update was cancelled before CORSIA restarted.'],
	] as const)('maps D3 %s to one plain sentence', (code, headline) => {
		expect(failureCopy(failure(code)).headline).toBe(headline);
	});

	it('keeps an unmapped failure reason out of the summary before restart', () => {
		const copy = failureCopy(failure(null));
		expect(copy.title).toBe("Update didn't start");
		expect(copy.headline).toBe('The update ended before CORSIA restarted.');
		expect(copy.body).toBe('CORSIA kept running and nothing was changed.');
		expect(copy.headline).not.toContain('Stored failure summary.');
	});

	it('does not claim nothing changed after restart began', () => {
		const copy = failureCopy({ ...failure(null), phase: 'restarting' });
		expect(copy.title).toBe("The update didn't finish");
		expect(copy.body).toBe('');
	});

	it('shows both disk figures and the action amount', () => {
		const copy = failureCopy({
			...failure('disk_space'),
			detail: JSON.stringify({ needed_bytes: 16_000_000_000, free_bytes: 4_000_000_000 }),
		});
		expect(copy.body).toContain('Needs 16 GB free; this Mac has 4 GB.');
		expect(copy.toastLine).toBe('Needs 12 GB more free space');
		expect(copy.retryLabel).toBe('Try again after freeing space');
	});

	it('holds queued input only after an update takes ownership of the drain', () => {
		const operation: UpdateOperation = { operation_id: 'u1', kind: 'update', status: 'in_flight', phase: 'holding' };
		expect(holdsNewWork(operation)).toBe(false);
		expect(holdsNewWork({ ...operation, phase: 'draining' })).toBe(true);
		expect(holdsNewWork({ ...operation, kind: 'restart', phase: 'draining' })).toBe(false);
	});
});
