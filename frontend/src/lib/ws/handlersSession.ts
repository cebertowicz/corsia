import { threads } from '../stores/threads';
import { patchUnlistedThreadRow } from '../stores/threadRow';
import { acceptThreadModelTruth, seedThreadModelTruth } from '../stores/models';
import { patchSubagentThread, registerSubagent } from '../stores/subagents';
import { acceptsThreadAccountFrame, threadAccountFields } from '../stores/threadVendorAccounts';
import { threadFrameFields, threadSwitchFields } from './threadFrameFields';

/**
 * A `session_updated` frame: the thread's title, last message, model truth and switch state.
 * A worker is not in the thread list, so the frame also lands on the worker's own row, which its composer reads.
 */
export function handleSessionUpdated(data: any) {
	if (!acceptsThreadAccountFrame(data)) return;
	const carriesModelTruth = data.model !== undefined;
	if (carriesModelTruth) {
		if (
			typeof data.model !== 'string'
			|| !data.model
			|| typeof data.updated_at !== 'string'
			|| !data.updated_at
		) {
			console.warn('Ignoring session_updated without complete model truth');
			return;
		}
		if (!acceptThreadModelTruth(
			data.thread_id,
			data.model,
			data.updated_at,
		)) return;
	}
	patchSubagentThread(data.thread_id, threadSwitchFields(data));
	patchUnlistedThreadRow(data.thread_id, threadSwitchFields(data));
	threads.update(ss => {
		const updated = ss.map(s => {
			if (s.id !== data.thread_id) return s;
			return {
				...s,
				...(data.title !== undefined ? { title: data.title } : {}),
				...(data.last_message !== undefined ? { last_message: data.last_message } : {}),
				...threadFrameFields(data),
				...threadAccountFields(data),
				...(!carriesModelTruth && data.updated_at !== undefined
					? { updated_at: data.updated_at }
					: {}),
			};
		});
		if (!carriesModelTruth && data.updated_at !== undefined) {
			updated.sort((a, b) => {
				const ta = a.updated_at ? new Date(a.updated_at.endsWith('Z') ? a.updated_at : a.updated_at + 'Z').getTime() : 0;
				const tb = b.updated_at ? new Date(b.updated_at.endsWith('Z') ? b.updated_at : b.updated_at + 'Z').getTime() : 0;
				return tb - ta;
			});
		}
		return updated;
	});
}

/**
 * A worker's `subagent_meta` frame: its row, and the model it is selected on, which is held where
 * every thread's is and seeded first so readers of the row see it.
 */
export function handleSubagentMeta(data: any) {
	if (!data.thread_id) return;
	if (data.model) seedThreadModelTruth(data.thread_id, data.model);
	registerSubagent(data.parent_thread_id, data.thread_id, data.name || 'subagent', data.provider || 'cc', data.live ? 'running' : 'idle');
}
