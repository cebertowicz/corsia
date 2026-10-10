import * as Y from 'yjs';
import { DeviceDocument } from './deviceDocument';

export interface DeviceRecovery { storeId: string; text: string }

/** Enumerate retained copies; never apply old-store structs to a new document. */
export async function readDeviceRecovery(artifactId: string, storeId: string): Promise<DeviceRecovery[]> {
	const prefix = `corsia-document:${encodeURIComponent(artifactId)}:`;
	const databases = await indexedDB.databases();
	const copies: DeviceRecovery[] = [];
	for (const database of databases) {
		if (!database.name?.startsWith(prefix)) continue;
		const oldId = decodeURIComponent(database.name.slice(prefix.length));
		if (oldId === storeId) continue;
		const doc = new Y.Doc();
		const copy = new DeviceDocument(artifactId, oldId, doc);
		try {
			await copy.ready;
			if (copy.error) throw new Error(copy.error);
			copies.push({ storeId: oldId, text: doc.getText('text').toString() });
		} finally {
			await copy.destroy();
			doc.destroy();
		}
	}
	return copies;
}
