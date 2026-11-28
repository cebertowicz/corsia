import { markDocumentsRead, type ActivityQueueItem } from '$lib/api/activityQueue';
import { emitProductEvent } from '$lib/product_events';
import { chatDetailOf, readThrough } from './activityItems';

/** Owns the read controls and their failures for the current Activity scope. */
export class ActivityRead {
	dismissFailed = $state<Record<string, string>>({});
	markAllFailed = $state(false);
	markingAll = $state(false);

	constructor(private context: {
		notifications: () => readonly ActivityQueueItem[];
		openKey: () => string | null;
		paneInstance: () => number;
		acted: (instance: number) => Promise<void>;
		reload: () => Promise<void>;
	}) {}

	/** × or a swipe marks the represented replies read without opening them. */
	async dismiss(key: string) {
		this.markAllFailed = false;
		const item = this.context.notifications().find((row) => row.key === key);
		const read = item && readThrough(item);
		const chat = item && chatDetailOf(item);
		if (!read && !chat) return;
		const { [key]: _, ...rest } = this.dismissFailed;
		this.dismissFailed = rest;
		try {
			await markDocumentsRead(read ? [read] : [], chat ? [{ thread_id: chat.thread_id, through: chat.through }] : []);
		} catch {
			this.dismissFailed = { ...this.dismissFailed, [key]: 'Couldn’t mark these replies read. Try again.' };
			return;
		}
		emitProductEvent('activity', 'dismissed');
		if (key === this.context.openKey()) await this.context.acted(this.context.paneInstance());
		else await this.context.reload();
	}

	/** Mark every listed document and chat read in one call. */
	async markAllRead() {
		if (this.markingAll) return;
		const notifications = this.context.notifications();
		const documents = notifications.map(readThrough).filter((read) => read !== null);
		const chats = notifications.map(chatDetailOf).filter((chat) => chat !== null).map(({ thread_id, through }) => ({ thread_id, through }));
		if (!documents.length && !chats.length) return;
		this.markingAll = true;
		this.markAllFailed = false;
		try {
			await markDocumentsRead(documents, chats);
			emitProductEvent('activity', 'marked_all_read', { count: documents.length + chats.length });
			await this.context.reload();
		} catch {
			this.markAllFailed = true;
		} finally {
			this.markingAll = false;
		}
	}
}
