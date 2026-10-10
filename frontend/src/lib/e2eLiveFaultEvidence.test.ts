import { describe, expect, it } from 'vitest';
import { matchesLiveFaultRestart, observeFaultTraffic, type ArmedLiveFault, type LiveFaultStatus } from '../../e2e/support/liveFault';
import type { BrowserContext, WebSocketRoute } from '@playwright/test';

const armed: ArmedLiveFault = { artifact_id: 'document', case_id: 'case', pid: 10, run_id: 'run', stage: 'choice_apply', generation: 1 };
const restarted: Partial<LiveFaultStatus> = { run_id: 'run', case_id: 'case', pid: 11, restart_count: 1, armed: false };

describe('live-fault evidence contracts', () => {
	it('accepts only the same run and case with exactly one restart and a positive new PID', () => {
		expect(matchesLiveFaultRestart(restarted, armed)).toBe(true);
		for (const invalid of [null, {}, { ...restarted, pid: 10 }, { ...restarted, pid: 0 }, { ...restarted, pid: null },
			{ ...restarted, case_id: 'other' }, { ...restarted, run_id: 'other' },
			{ ...restarted, restart_count: 0 }, { ...restarted, restart_count: 2 }, { ...restarted, armed: true }]) {
			expect(matchesLiveFaultRestart(invalid as Partial<LiveFaultStatus> | null, armed)).toBe(false);
		}
	});

	it('tracks immutable message IDs and scopes held ACKs to the old socket and store', async () => {
		let route: (socket: WebSocketRoute) => void = () => { throw new Error('route not installed'); };
		const context = { routeWebSocket: async (_url: unknown, handler: typeof route) => { route = handler; } };
		const traffic = await observeFaultTraffic(context as unknown as BrowserContext);
		function connection() {
			let upstream: (payload: string) => void = () => {};
			let downstream: (payload: string) => void = () => {};
			const delivered: string[] = [];
			const server = { onMessage: (handler: typeof downstream) => { downstream = handler; }, onClose: () => {}, send: () => {}, close: async () => {} };
			const socket = { connectToServer: () => server, onMessage: (handler: typeof upstream) => { upstream = handler; }, onClose: () => {}, send: (payload: string) => delivered.push(payload), close: async () => {} };
			route(socket as unknown as WebSocketRoute);
			return { sent: (value: object) => upstream(JSON.stringify(value)), received: (value: object) => downstream(JSON.stringify(value)), delivered };
		}
		const first = connection();
		first.sent({ type: 'doc_update', store_id: 'store', message_id: 'update' });
		traffic.holdCurrentSocketAcks();
		first.received({ type: 'doc_ack', store_id: 'store', message_ids: ['update'] });
		expect(first.delivered).toEqual([]);
		expect(traffic.acknowledged('store', 'update')).toBe(0);
		const second = connection();
		second.sent({ type: 'doc_sync', store_id: 'store', message_id: 'resync' });
		second.received({ type: 'doc_ack', store_id: 'other-store', message_ids: ['resync'] });
		expect(traffic.acknowledged('store', 'resync')).toBe(0);
		second.received({ type: 'doc_ack', store_id: 'store', message_ids: ['resync'] });
		expect(traffic.sent('store', 'update')).toBe(1);
		expect(traffic.acknowledged('store', 'update')).toBe(0);
		expect(traffic.sent('store', 'resync')).toBe(1);
		expect(traffic.syncIds('store')).toEqual(['resync']);
		expect(traffic.acknowledged('store', 'resync')).toBe(1);
		expect(second.delivered).toHaveLength(2);
	});

	it('distinguishes the first reopened sync from choice and queued-flush traffic', async () => {
		let route: (socket: WebSocketRoute) => void = () => { throw new Error('route not installed'); };
		const context = { routeWebSocket: async (_url: unknown, handler: typeof route) => { route = handler; } };
		const traffic = await observeFaultTraffic(context as unknown as BrowserContext);
		function connection() {
			let upstream: (payload: string) => void = () => {};
			let downstream: (payload: string) => void = () => {};
			const server = { onMessage: (handler: typeof downstream) => { downstream = handler; }, onClose: () => {}, send: () => {}, close: async () => {} };
			const socket = { connectToServer: () => server, onMessage: (handler: typeof upstream) => { upstream = handler; }, onClose: () => {}, send: () => {}, close: async () => {} };
			route(socket as unknown as WebSocketRoute);
			return { sent: (value: object) => upstream(JSON.stringify(value)), received: (value: object) => downstream(JSON.stringify(value)) };
		}
		const first = connection();
		first.sent({ type: 'doc_sync', artifact_id: 'artifact', store_id: 'store', message_id: 'choice' });
		first.sent({ type: 'doc_sync', artifact_id: 'artifact', store_id: 'store', message_id: 'flush' });
		const second = connection();
		second.received({ type: 'doc_opened', artifact_id: 'artifact', store_id: 'store' });
		second.received({ type: 'doc_opened', artifact_id: 'artifact', store_id: 'store' });
		const otherSocket = connection();
		otherSocket.sent({ type: 'doc_sync', artifact_id: 'artifact', store_id: 'store', message_id: 'other-socket' });
		second.sent({ type: 'doc_sync', artifact_id: 'other-artifact', store_id: 'store', message_id: 'other-artifact' });
		second.sent({ type: 'doc_sync', artifact_id: 'artifact', store_id: 'other-store', message_id: 'other-store' });
		second.sent({ type: 'doc_sync', artifact_id: 'artifact', store_id: 'store', message_id: 'opened' });
		second.sent({ type: 'doc_sync', artifact_id: 'artifact', store_id: 'store', message_id: 'queued-flush' });
		expect(traffic.syncIds('store')).toEqual(['choice', 'flush', 'other-socket', 'other-artifact', 'opened', 'queued-flush']);
		expect(traffic.openedSyncIds('artifact', 'store')).toEqual(['opened', 'opened']);
		expect(traffic.sent('store', 'opened')).toBe(1);
		expect(traffic.acknowledged('store', 'opened')).toBe(0);
	});
});
