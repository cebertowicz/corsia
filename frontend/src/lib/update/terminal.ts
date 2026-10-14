import { toasts } from '$lib/stores/toast';
import { failureCopy, failureFromOperation, isFailedOperation, type UpdateOperation } from './model';
import { rememberId } from './releaseNotes';
import type { UpdateStatus } from './store';

interface TerminalOptions {
	operation: UpdateOperation;
	status: UpdateStatus | null;
	authToken: string | null;
	handledKey: string;
	surfaceClosed: boolean;
	onDetails: () => void;
}

export async function presentUpdateTerminal({
	operation, status, authToken, handledKey, surfaceClosed, onDetails,
}: TerminalOptions): Promise<void> {
	if (operation.status === 'complete') {
		const version = operation.target_version ?? status?.target_version ?? 'the latest version';
		const overnight = operation.trigger === 'tonight' || operation.trigger === 'automatic';
		if (overnight) {
			if (!authToken) return;
			const response = await fetch(`/api/system/update-operation/${operation.operation_id}/claim-overnight-outcome`, {
				method: 'POST', headers: { Authorization: `Bearer ${authToken}` }, cache: 'no-store',
			});
			if (!response.ok || !(await response.json() as { claimed: boolean }).claimed) return;
			const resumed = operation.drain_total ?? 0;
			toasts.pin('update', {
				type: 'success',
				message: `Updated to ${version} overnight${resumed > 0 ? ` · ${resumed} ${resumed === 1 ? 'agent' : 'agents'} resumed` : ''}`,
				action: { label: "See what's new", run: onDetails },
			});
			return;
		}
		if (!rememberId(handledKey, operation.operation_id, 'browser')) return;
		toasts.pin('update', {
			type: 'success', message: `Updated to ${version}`,
			action: { label: "See what's new", run: onDetails },
		});
		return;
	}
	if (!rememberId(handledKey, operation.operation_id) || !isFailedOperation(operation) || !surfaceClosed) return;
	const copy = failureCopy(failureFromOperation(operation));
	toasts.pin('update', {
		type: 'error', message: copy.title, detail: copy.toastLine ?? undefined,
		action: { label: 'Details', run: onDetails },
	});
}
