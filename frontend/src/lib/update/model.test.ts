import { describe, expect, it } from 'vitest';
import { failureCopy, failureFromVerdict } from './failureCopy';
import { formatScheduleTime, holdsNewWork, stepDetail, type UpdateFailure, type UpdateOperation } from './model';

const failure = (code: UpdateFailure['code']): UpdateFailure => ({
	code, detail: null, summary: 'Stored failure summary.', conductorTold: false, log: null,
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

	it.each([
		['drain_failed', "Couldn't bring your agents to a safe stopping point.", 'Try again'],
		['update_timed_out', 'The update took too long to download and build.', 'Try again'],
		['prepare_failed', "Couldn't prepare the update.", 'Try again'],
		['index_locked', "Another Git process is holding this install's lock.", 'Try again after clearing it'],
		['checkout_detached', "This install isn't on a branch.", null],
		['checkout_dirty', "This install has changes that aren't committed.", 'Try again after cleaning up'],
		['restart_pending', 'An update is already downloaded and waiting for a restart.', 'Restart now'],
	] as const)('words %s truthfully and names its action', (code, headline, retryLabel) => {
		const copy = failureCopy(failure(code));
		expect(copy.headline).toBe(headline);
		expect(copy.retryLabel).toBe(retryLabel);
	});

	it('sends restart_pending to a restart and every other failure to another update', () => {
		expect(failureCopy(failure('restart_pending')).retryAction).toBe('restart');
		expect(failureCopy(failure('drain_failed')).retryAction).toBeUndefined();
	});

	it('claims nothing was changed only for refusals made before anything was touched', () => {
		for (const code of ['index_locked', 'checkout_detached', 'checkout_dirty'] as const) {
			expect(failureCopy(failure(code)).body).toContain('nothing was changed');
		}
		for (const code of ['drain_failed', 'update_timed_out', 'prepare_failed', 'restart_pending'] as const) {
			expect(failureCopy(failure(code)).body).not.toContain('nothing was changed');
		}
	});

	it('keeps an unmapped failure reason out of the summary and claims nothing about how far the update got', () => {
		const copy = failureCopy(failure(null));
		expect(copy.title).toBe("Update didn't finish");
		expect(copy.headline).toBe('The update stopped before it finished.');
		expect(copy.body).toBe('');
		expect(copy.retryLabel).toBe('Try again');
		expect(copy.headline).not.toContain('Stored failure summary.');
	});

	it('claims nothing was changed only where nothing was touched', () => {
		const untouched = ['disk_space', 'fetch_failed'] as const;
		const touched = ['build_failed', 'drain_cancelled', 'launch_failed', null] as const;
		for (const code of untouched) expect(failureCopy(failure(code)).body).toContain('nothing was changed');
		for (const code of touched) expect(failureCopy(failure(code)).body).not.toContain('nothing was changed');
		expect(failureCopy(failure('build_failed')).body).toBe('CORSIA is still running the version it had.');
	});

	it('gives a client verdict its own copy, never the operation copy', () => {
		const mismatch = failureCopy({ ...failure(null), client: { kind: 'version_mismatch', message: 'Versions differ.' } });
		expect(mismatch.title).toBe("The update didn't finish");
		expect(mismatch.headline).toBe('The update finished, but the running versions do not match.');
		expect(mismatch.body).toBe('Versions differ.');
		const refused = failureCopy(failureFromVerdict({ kind: 'refused', message: 'A restart is already in flight.' }));
		expect(refused.title).toBe("Update didn't start");
		expect(refused.headline).toBe('A restart is already in flight.');
		expect(refused.headline).not.toContain('versions do not match');
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
