import { updateThreadRow } from './threadRow';

export interface ThreadModelSwitchState {
	pending_model?: string | null;
	switch_status?: 'preparing' | 'ready' | 'failed' | 'waiting_for_turn' | string | null;
	switch_error?: string | null;
	switch_operation_id?: string | null;
}

export interface PendingThreadModelSaveResult {
	kind: 'pending';
	operationId: string;
	status: string;
	pendingModel: string;
	checkpointRequired: boolean;
}

export function applyPendingThreadModelSwitch(
	threadId: string,
	body: any,
): PendingThreadModelSaveResult {
	if (
		typeof body?.operation_id !== 'string'
		|| !body.operation_id
		|| typeof body?.status !== 'string'
		|| !body.status
		|| typeof body?.pending_model !== 'string'
		|| !body.pending_model
		|| typeof body?.checkpoint_required !== 'boolean'
	) {
		throw new Error('Model switch response did not include pending operation truth');
	}
	const pending = {
		pending_model: body.pending_model,
		switch_status: body.status,
		switch_error: null,
		switch_operation_id: body.operation_id,
	};
	updateThreadRow(threadId, pending);
	return {
		kind: 'pending',
		operationId: body.operation_id,
		status: body.status,
		pendingModel: body.pending_model,
		checkpointRequired: body.checkpoint_required,
	};
}
