import { expect, type APIRequestContext, type BrowserContext, type WebSocketRoute } from '@playwright/test';

export interface ArmedLiveFault {
	artifact_id: string;
	case_id: string;
	pid: number;
	run_id: string;
	stage: string;
	generation: number;
}

export interface LiveFaultStatus {
	artifact_id: string;
	store_id: string;
	pid: number;
	run_id: string;
	case_id: string;
	restart_count: number;
	armed: boolean;
	received_ids: string[];
	pending: string | null;
	choice: string | null;
}

export interface LiveFaultEvidence extends LiveFaultStatus {
	live_text: string;
	durable_text: string;
	mirror_text: string;
	file_text: string;
	live_state_digest: string;
	durable_state_digest: string;
	mirror_state_digest: string;
}

export function liveFaultsEnabled(): boolean {
	return process.env.CORSIA_E2E_MOCK === '1'
		&& process.env.CORSIA_E2E_LIVE_FAULTS === '1'
		&& process.env.CORSIA_E2E_FRESH_ROOTS === '1';
}

export async function prepareHeldDocument(request: APIRequestContext, artifactId: string, outsideText: string): Promise<LiveFaultStatus> {
	const response = await request.post(`/api/test/live-faults/${artifactId}/prepare-held`, { data: { outside_text: outsideText } });
	expect(response.ok(), await response.text()).toBeTruthy();
	return await response.json() as LiveFaultStatus;
}

export async function beginLiveFaultCase(request: APIRequestContext): Promise<void> {
	const response = await request.post('/api/test/live-faults/case');
	expect(response.ok(), await response.text()).toBeTruthy();
}

export async function liveFaultStatus(request: APIRequestContext, artifactId: string): Promise<LiveFaultStatus> {
	const response = await request.get(`/api/test/live-faults/status?artifact_id=${encodeURIComponent(artifactId)}`);
	expect(response.ok(), await response.text()).toBeTruthy();
	return await response.json() as LiveFaultStatus;
}

export async function liveFaultEvidence(request: APIRequestContext, artifactId: string): Promise<LiveFaultEvidence> {
	const response = await request.get(`/api/test/live-faults/${artifactId}/evidence`);
	expect(response.ok(), await response.text()).toBeTruthy();
	return await response.json() as LiveFaultEvidence;
}

export async function refuseLiveFaultOnce(request: APIRequestContext, artifactId: string, kind: 'commit' | 'record' | 'apply' | 'rename' | 'clear'): Promise<void> {
	const response = await request.post(`/api/test/live-faults/${artifactId}/refuse/${kind}`);
	expect(response.ok(), await response.text()).toBeTruthy();
}

export async function armLiveFault(request: APIRequestContext, artifactId: string, stage: string): Promise<ArmedLiveFault> {
	const response = await request.post(`/api/test/live-faults/${artifactId}/arm/${stage}`);
	expect(response.ok(), await response.text()).toBeTruthy();
	return await response.json() as ArmedLiveFault;
}

export function matchesLiveFaultRestart(state: Partial<LiveFaultStatus> | null, armed: ArmedLiveFault): boolean {
	return state !== null && state.run_id === armed.run_id && state.case_id === armed.case_id
		&& state.restart_count === 1 && Number.isInteger(state.pid) && Number(state.pid) > 0
		&& state.pid !== armed.pid && state.armed === false;
}

export async function waitForLiveFaultRestart(request: APIRequestContext, armed: ArmedLiveFault): Promise<void> {
	await expect.poll(async () => {
		try {
			const response = await request.get('/api/test/live-faults/status');
			if (!response.ok()) return false;
			const state = await response.json() as LiveFaultStatus;
			return matchesLiveFaultRestart(state, armed);
		} catch { return false; }
	}, { timeout: 30_000, message: 'Expected same run/case, one restart and a new live child' }).toBe(true);
}

interface DocumentFrame {
	type?: string;
	artifact_id?: string;
	store_id?: string;
	message_id?: string;
	message_ids?: string[];
}

export interface FaultTraffic {
	acknowledged(storeId: string, messageId: string): number;
	sent(storeId: string, messageId: string): number;
	syncIds(storeId: string): string[];
	openedSyncIds(artifactId: string, storeId: string): string[];
	holdCurrentSocketAcks(): void;
}

/** Observe real traffic; withhold only the old socket's ACK to force genuine resend. */
export async function observeFaultTraffic(context: BrowserContext): Promise<FaultTraffic> {
	const sockets = new Set<WebSocketRoute>();
	const held = new Set<WebSocketRoute>();
	const sent = new Map<string, number>();
	const acked = new Map<string, number>();
	const syncs = new Map<string, string[]>();
	const openedSyncs = new Map<string, string[]>();
	const pendingOpened = new Map<WebSocketRoute, Array<{ artifactId: string; storeId: string }>>();
	const key = (storeId: string, id: string) => JSON.stringify([storeId, id]);
	const openedKey = (artifactId: string, storeId: string) => JSON.stringify([artifactId, storeId]);
	const parse = (payload: string | Buffer): DocumentFrame | null => {
		try { return JSON.parse(String(payload)) as DocumentFrame; } catch { return null; }
	};
	await context.routeWebSocket(url => url.pathname === '/ws', socket => {
		const server = socket.connectToServer();
		sockets.add(socket);
		socket.onMessage(payload => {
			const frame = parse(payload);
			if ((frame?.type === 'doc_update' || frame?.type === 'doc_sync') && frame.store_id && frame.message_id) {
				const id = key(frame.store_id, frame.message_id);
				sent.set(id, (sent.get(id) ?? 0) + 1);
				if (frame.type === 'doc_sync') {
					const ids = syncs.get(frame.store_id) ?? [];
					ids.push(frame.message_id);
					syncs.set(frame.store_id, ids);
					if (frame.artifact_id) {
						const opens = pendingOpened.get(socket) ?? [];
						const matching = opens.filter(open => open.artifactId === frame.artifact_id && open.storeId === frame.store_id);
						if (matching.length > 0) {
							const openedIds = openedSyncs.get(openedKey(frame.artifact_id, frame.store_id)) ?? [];
							for (const _open of matching) openedIds.push(frame.message_id);
							openedSyncs.set(openedKey(frame.artifact_id, frame.store_id), openedIds);
							const remaining = opens.filter(open => open.artifactId !== frame.artifact_id || open.storeId !== frame.store_id);
							if (remaining.length > 0) pendingOpened.set(socket, remaining);
							else pendingOpened.delete(socket);
						}
					}
				}
			}
			server.send(payload);
		});
		server.onMessage(payload => {
			const frame = parse(payload);
			if (frame?.type === 'doc_opened' && frame.artifact_id && frame.store_id) {
				const opens = pendingOpened.get(socket) ?? [];
				opens.push({ artifactId: frame.artifact_id, storeId: frame.store_id });
				pendingOpened.set(socket, opens);
			}
			if (frame?.type === 'doc_ack' && frame.store_id) {
				if (held.has(socket)) return;
				for (const messageId of frame.message_ids ?? []) {
					const id = key(frame.store_id, messageId);
					acked.set(id, (acked.get(id) ?? 0) + 1);
				}
			}
			socket.send(payload);
		});
		server.onClose(async (code, reason) => {
			sockets.delete(socket);
			held.delete(socket);
			pendingOpened.delete(socket);
			await socket.close({ code: code === 1006 ? 1001 : code, reason });
		});
		socket.onClose(async (code, reason) => {
			sockets.delete(socket);
			held.delete(socket);
			pendingOpened.delete(socket);
			await server.close({ code: code === 1006 ? 1001 : code, reason });
		});
	});
	return {
		acknowledged: (storeId, id) => acked.get(key(storeId, id)) ?? 0,
		sent: (storeId, id) => sent.get(key(storeId, id)) ?? 0,
		syncIds: storeId => [...(syncs.get(storeId) ?? [])],
		openedSyncIds: (artifactId, storeId) => [...(openedSyncs.get(openedKey(artifactId, storeId)) ?? [])],
		holdCurrentSocketAcks: () => { for (const socket of sockets) held.add(socket); },
	};
}
