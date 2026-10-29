import { describe, expect, it } from 'vitest';
import { failureCopy, failureFromVerdict } from './failureCopy';
import { changeList, costLine, currentOperation, formatScheduleTime, holdsNewWork, plainChangeTitle, stepIndex, stepMessage, type UpdateFailure, type UpdateOperation } from './model';

const failure = (code: UpdateFailure['code']): UpdateFailure => ({
	code, detail: null, summary: 'Stored failure summary.', conductorTold: false, log: null,
});

describe('update model', () => {
	it('formats every configured schedule time in 12-hour time', () => {
		expect(formatScheduleTime('03:00')).toBe('3:00 AM');
		expect(formatScheduleTime('15:05')).toBe('3:05 PM');
		expect(formatScheduleTime('00:00')).toBe('12:00 AM');
	});

	it('words each step in the toast phrases, with the safe-point count when agents are draining', () => {
		const op = { operation_id: 'u', kind: 'update', status: 'in_flight', phase: 'holding' } as UpdateOperation;
		expect(stepMessage(op, false)).toBe('Installing update');
		const draining = { ...op, phase: 'draining', drain_total: 3, blocking: 2 } as UpdateOperation;
		expect(stepMessage(draining, false)).toBe('Pausing agents \u00b7 1 of 3');
		expect(stepMessage({ ...draining, drain_total: 0 } as UpdateOperation, false)).toBe('Pausing agents');
		expect(stepMessage({ ...op, phase: 'restarting' } as UpdateOperation, false)).toBe('Restarting CORSIA');
		expect(stepMessage({ ...op, phase: 'resuming', drain_total: 1 } as UpdateOperation, false)).toBe('Resuming 1 agent');
		expect(stepMessage({ ...op, phase: 'resuming', drain_total: 3 } as UpdateOperation, false)).toBe('Resuming 3 agents');
		expect(stepMessage({ ...op, phase: 'resuming', drain_total: 0 } as UpdateOperation, false)).toBe('Resuming agents');
		expect(stepMessage(op, true)).toBe('Restarting CORSIA');
	});

	it('names no step for a settled operation or a phase it cannot place', () => {
		const settled = { operation_id: 'u', kind: 'update', status: 'complete', phase: null } as UpdateOperation;
		expect(stepIndex(settled, false)).toBeNull();
		expect(stepMessage(settled, false)).toBe('Updating');
		const unknown = { operation_id: 'u', kind: 'update', status: 'in_flight', phase: 'unplaceable' } as unknown as UpdateOperation;
		expect(stepIndex(unknown, false)).toBeNull();
	});

	it('words the cost as one sentence for 0, 1 and several working agents', () => {
		expect(costLine(60, 0)).toBe('CORSIA restarts to finish.');
		expect(costLine(1, 1)).toBe('The restart usually takes 1 second; 1 working agent pauses and picks up where it left off.');
		expect(costLine(60, 1)).toBe('The restart usually takes a minute; 1 working agent pauses and picks up where it left off.');
		expect(costLine(120, 40)).toBe('The restart usually takes 2 minutes; 40 working agents pause and pick up where they left off.');
		expect(costLine(null, 3)).toBe('CORSIA restarts to finish; 3 working agents pause and pick up where they left off.');
	});

	it("lists release notes as written and cleans a development build's commit titles", () => {
		expect(plainChangeTitle('[Infrastructure] fix the lock message (#123)')).toBe('Fix the lock message');
		expect(changeList({ kind: 'changelog', headline: null, items: ['Faster search'], more: 0 })).toEqual({ heading: "What's new", items: ['Faster search'] });
		expect(changeList({ kind: 'commits', headline: null, items: ['[Bugs] tidy the list (#9)'], more: 0 })).toEqual({ heading: 'Recent changes', items: ['Tidy the list'] });
		expect(changeList({ kind: 'commits', headline: null, items: [], more: 0 })).toBeNull();
		expect(changeList(null)).toBeNull();
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
		['drain_failed', "Your agents didn't reach a safe stopping point in time.", 'Retry'],
		['update_timed_out', 'The update took too long to download and build.', 'Retry'],
		['prepare_failed', "Couldn't prepare the update.", 'Retry'],
		['index_locked', "Another program is using CORSIA's files.", 'Retry', 'Wait for it to finish, then select Retry. If it doesn’t clear, ask the CORSIA agent to remove the leftover lock. CORSIA kept running and nothing was changed.'],
		['checkout_detached', "This copy of CORSIA can't update itself because it isn't set up to follow updates.", null],
		['checkout_dirty', 'This copy of CORSIA has local changes.', 'Retry', 'Save or discard them, then select Retry. CORSIA kept running and nothing was changed.'],
		['checkout_no_upstream', "This branch isn't set to track another branch.", 'Retry', "Ask the CORSIA agent to set the branch it should track, then select Retry. CORSIA kept running and your code wasn't changed."],
		['checkout_diverged', 'This branch and the branch it tracks have both changed.', 'Retry', "Ask the CORSIA agent to combine their changes, then select Retry. CORSIA kept running and your code wasn't changed."],
		['restart_pending', 'An update is ready and waiting for a restart.', 'Restart now'],
		['rolled_back', "Couldn't start the new version.", 'Retry'],
		['version_unverified', "Couldn't confirm the new version.", 'Retry', "The update's last check didn't pass, so CORSIA can't tell which version is running."],
		['wrong_version_running', 'CORSIA started a different version.', 'Retry', "It isn't the version this update was for."],
	] as const)('words %s truthfully and names its action', (code, headline, retryLabel, body) => {
		const copy = failureCopy(failure(code));
		expect(copy.headline).toBe(headline);
		expect(copy.retryLabel).toBe(retryLabel);
		if (body) expect(copy.body).toBe(body);
	});

	it.each([
		['checkout_no_upstream', 'has no upstream', "The branch my-feature isn't set to track another branch."],
		['checkout_diverged', 'and its upstream have diverged', 'The branch my-feature and the branch it tracks have both changed.'],
	] as const)('names the branch in %s when its log is available', (code, cause, headline) => {
		const result = failureCopy({ ...failure(code), log: `Redeploy refused: branch my-feature ${cause} — checkout left untouched` });
		expect(result.headline).toBe(headline);
	});

	it('offers a restart when it can recover an unfinished update', () => {
		expect(failureCopy(failure('restart_pending')).retryAction).toBe('restart');
		expect(failureCopy(failure('drain_failed')).retryAction).toBeUndefined();
	});

	it.each(['version_unverified', 'wrong_version_running'] as const)('never claims the previous version is running for %s', (code) => {
		const copy = failureCopy(failure(code));
		expect(copy.body).not.toContain('the version it had');
		expect(copy.body).not.toContain('nothing was changed');
		expect(copy.headline).not.toMatch(/rollback|verify|come up/);
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
		expect(copy.retryLabel).toBe('Retry');
		expect(copy.headline).not.toContain('Stored failure summary.');
	});

	it('claims nothing was changed only where nothing was touched', () => {
		const untouched = ['disk_space', 'fetch_failed'] as const;
		const touched = ['build_failed', 'drain_cancelled', 'launch_failed', 'rolled_back', null] as const;
		for (const code of untouched) expect(failureCopy(failure(code)).body).toContain('nothing was changed');
		for (const code of touched) expect(failureCopy(failure(code)).body).not.toContain('nothing was changed');
		expect(failureCopy(failure('build_failed')).body).toBe('CORSIA is still running the version it had.');
	});

	it('gives a client verdict its own copy, never the operation copy', () => {
		const mismatch = failureCopy({ ...failure(null), client: { kind: 'version_mismatch', message: 'Versions differ.' } });
		expect(mismatch.title).toBe("The update didn't finish");
		expect(mismatch.headline).toBe('The update finished, but CORSIA still shows the old version.');
		expect(mismatch.body).toBe('Versions differ.');
		const rolledBack = failureCopy(failure('rolled_back'));
		expect(rolledBack.title).toBe("Update didn't finish");
		expect(rolledBack.headline).toBe("Couldn't start the new version.");
		expect(rolledBack.body).toBe('CORSIA is still running the version it had.');
		expect(rolledBack.retryLabel).toBe('Retry');
		const refused = failureCopy(failureFromVerdict({ kind: 'refused', message: 'A restart is already in flight.' }));
		expect(refused.title).toBe("Update didn't start");
		expect(refused.headline).toBe('A restart is already in flight.');
		expect(refused.headline).not.toContain('old version');
	});

	it('shows both disk figures and the action amount', () => {
		const copy = failureCopy({
			...failure('disk_space'),
			detail: JSON.stringify({ needed_bytes: 16_000_000_000, free_bytes: 4_000_000_000 }),
		});
		expect(copy.body).toBe('Needs 16 GB free; this computer has 4 GB. Free up space, then select Retry. CORSIA kept running and nothing was changed.');
		expect(copy.toastLine).toBe('Needs 12 GB more free space');
		expect(copy.retryLabel).toBe('Retry');
	});

	it('gives the next step when disk figures are unavailable', () => {
		const copy = failureCopy(failure('disk_space'));
		expect(copy.body).toBe('Free up space, then select Retry. CORSIA kept running and nothing was changed.');
		expect(copy.retryLabel).toBe('Retry');
	});

	it('holds queued input only after an update takes ownership of the drain', () => {
		const operation: UpdateOperation = { operation_id: 'u1', kind: 'update', status: 'in_flight', phase: 'holding' };
		expect(holdsNewWork(operation)).toBe(false);
		expect(holdsNewWork({ ...operation, phase: 'draining' })).toBe(true);
		expect(holdsNewWork({ ...operation, phase: 'verifying' })).toBe(false);
		expect(stepMessage({ ...operation, phase: 'verifying' }, false)).toBe('Checking the new version');
		expect(holdsNewWork({ ...operation, kind: 'restart', phase: 'draining' })).toBe(false);
	});

	describe('currentOperation', () => {
		const failed = (extra: Partial<UpdateOperation> = {}): UpdateOperation => ({
			operation_id: 'u1', kind: 'update', status: 'failed', failure_code: 'restart_pending', settled_at: '2026-10-13T22:00:00Z', ...extra,
		});

		it('keeps a failed update current until a restart has activated it', () => {
			expect(currentOperation(failed(), null)?.operation_id).toBe('u1');
			expect(currentOperation(failed({ activated_by_restart: false }), null)?.operation_id).toBe('u1');
		});

		it('treats a staged update that a restart activated as history', () => {
			expect(currentOperation(failed({ activated_by_restart: true }), null)).toBeNull();
		});

		it('still shows every other failure and a running operation', () => {
			expect(currentOperation(failed({ failure_code: 'build_failed' }), null)?.failure_code).toBe('build_failed');
			const running: UpdateOperation = { operation_id: 'u2', kind: 'update', status: 'in_flight', phase: 'draining' };
			expect(currentOperation(running, null)).toBe(running);
		});
	});
});
