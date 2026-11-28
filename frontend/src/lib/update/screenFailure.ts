import { get } from 'svelte/store';
import { failureCopy, failureFromOperation, failureFromVerdict, runningVersionOf, type FailureCopy } from './failureCopy';
import { isFailedOperation, isResolvedFailure, type UpdateFailureCode, type UpdateOperation } from './model';
import { updateFailureVerdict, updateStatus } from './store';

export type ScreenFailure = { code: UpdateFailureCode | null; detail: string | null; verdict: boolean };

export function currentFailure(previous: ScreenFailure | null, operation: UpdateOperation | null, dismissedId: string | null): ScreenFailure | null {
	if (operation?.operation_id === dismissedId) return null;
	const verdict = get(updateFailureVerdict);
	if (verdict?.kind === 'version_mismatch') return { code: null, detail: verdict.message, verdict: true };
	if (operation && isFailedOperation(operation) && !isResolvedFailure(operation)) {
		return { code: operation.failure_code ?? null, detail: operation.failure_detail ?? null, verdict: false };
	}
	return previous;
}

export function copyFor(failure: ScreenFailure, operation: UpdateOperation | null, serviceDown: boolean): FailureCopy {
	const source = failure.verdict ? failureFromVerdict({ kind: 'version_mismatch', message: failure.detail ?? '' })
		: operation && isFailedOperation(operation) ? failureFromOperation(operation)
			: { code: failure.code, detail: failure.detail, summary: null, conductorTold: false, log: null };
	return failureCopy(source, runningVersionOf(get(updateStatus), serviceDown));
}
