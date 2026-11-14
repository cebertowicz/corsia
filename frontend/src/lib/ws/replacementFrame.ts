import { messages } from '../stores/threads';
import { isDisplayedThread } from '../stores/displayedThreads';
import { addedMessage } from './messageProjection';
import { emit } from './events';
import { rememberReplacement, pruneReplacedRows } from '../messageReplacements';
import { compareMessageOrder } from '../messageHistory';

/** Apply removal and new row together, including displayed subagent leaves. */
export function handleReplacementFrame(data: any): void {
	const thread = typeof data?.thread_id === 'string' ? data.thread_id : null;
	if (!thread || !Array.isArray(data.removed_message_ids)
		|| !data.removed_message_ids.every((id: unknown) => typeof id === 'string')) return;
	const row = addedMessage(data.message, thread);
	if (!row) return;
	rememberReplacement(thread, data.removed_message_ids);
	if (isDisplayedThread(thread)) messages.update(current => pruneReplacedRows([
		...current.filter(message => message.id !== row.id), row,
	]).sort(compareMessageOrder));
	emit('messages_replaced', data);
}
