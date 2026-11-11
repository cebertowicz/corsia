import { messages, mergeMessageDelivery, type Message } from '../stores/threads';
import { isDisplayedThread } from '../stores/displayedThreads';
import { compareMessageOrder } from '../messageHistory';

export function reconcileOptimisticRow(
	threadId: string,
	inputId: string,
	messageId: string,
	createdAt: string,
	delivery: Pick<Message, 'delivery_state' | 'delivery_error' | 'delivery_revision' | 'seq'> = {},
): void {
	if (!isDisplayedThread(threadId)) return;
	messages.update(current => current.map(message =>
		(message.metadata?.input_id === inputId || message.id === inputId)
			? mergeMessageDelivery(message, {
				...message,
				id: messageId,
				created_at: createdAt,
				_optimistic: false,
				...delivery,
			})
			: message
	).sort(compareMessageOrder));
}
