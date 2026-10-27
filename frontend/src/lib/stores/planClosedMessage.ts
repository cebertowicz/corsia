import type { PlanKey } from './planKeys';

/** Explain conditions a retry cannot change in the selected place's terms. */
export function planClosedMessage(key: PlanKey, status: 'not-found' | 'forbidden'): string {
	const place = key.kind === 'project' || key.kind === 'activity-project' ? 'project'
		: key.kind === 'workspace' || key.kind === 'activity-workspace' ? 'workspace'
		: 'chat';
	return status === 'not-found'
		? `This ${place} isn’t available. Choose another.`
		: `You don’t have access to this ${place}.`;
}
