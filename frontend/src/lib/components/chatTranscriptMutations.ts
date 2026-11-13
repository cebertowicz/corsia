import { messageBody } from './chatUnreadTargets';

/** Whether a batch of transcript mutations changed message text or rows, so chat comment highlights must be painted again; a highlight dot's own churn never counts. */
export function transcriptTextChanged(records: MutationRecord[]): boolean {
	return records.some(record => {
		const target = record.target instanceof Element ? record.target : record.target.parentElement;
		if (target?.closest('[data-highlight-dot]')) return false;
		const nodes = [...record.addedNodes, ...record.removedNodes];
		const row = target?.closest('.message[data-message-id]') ?? null;
		if (target && messageBody(row)?.contains(target)) {
			return record.type === 'characterData' || nodes.some(node => !(node instanceof Element && node.matches('[data-highlight-dot]')));
		}
		return nodes.some(node => node instanceof Element && (
			node.matches('.message[data-message-id]') || !!node.querySelector('.message[data-message-id]') ||
			(!!row && (node.matches('.long-message-body, .bubble-content') || !!node.querySelector('.long-message-body, .bubble-content')))
		));
	});
}
