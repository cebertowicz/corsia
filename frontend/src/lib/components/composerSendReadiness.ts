import { acceptsSend, findModelById, modelAvailability, type Model } from '$lib/stores/models';
import type { Thread } from '$lib/stores/threads';

type ReadinessThread = Pick<Thread, 'model' | 'pending_model' | 'switch_status'>;

/** What one send did: `sent`, refused silently, or refused after the composer already told the person (`notified`). */
export type SendOutcome = 'sent' | 'not_sent' | 'notified';

/** A pending model switch whose destination can already run. */
export function hasReadySwitchDestination(
	thread: ReadinessThread | undefined,
	models: Model[],
): boolean {
	const pendingModel = thread?.pending_model;
	if (!pendingModel || thread?.switch_status === 'failed') return false;
	const destination = findModelById(models, pendingModel);
	return destination !== undefined && modelAvailability(destination).status === 'ready';
}

/**
 * Whether the composer hands a typed message to the server.
 *
 * A thread with no model yet takes it too: the server keeps it, shown as
 * "Choose a model to send", until the user chooses one. A model blocked only by a usage limit
 * takes it as well (`acceptsSend`): the server queues it and releases it in order on reset or move.
 */
export function composerCanSend(
	thread: ReadinessThread | undefined,
	activeModel: Model | undefined,
	models: Model[],
	allowUnlistedNoModelThread = false,
): boolean {
	if (activeModel && acceptsSend(activeModel)) return true;
	if ((thread || allowUnlistedNoModelThread) && !thread?.model && !thread?.pending_model) return true;
	return hasReadySwitchDestination(thread, models);
}
