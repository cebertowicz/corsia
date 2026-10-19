import { afterEach, describe, expect, it, vi } from 'vitest';

const save = vi.hoisted(() => vi.fn());
const fetchCorsiaAgent = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/models', () => ({ getModelDispatchId: () => 'codex/model', saveThreadModel: save }));
vi.mock('$lib/utils/conductor', () => ({ fetchCorsiaAgent }));

import { bindSetupFirstModel } from './setupFirstModel';

afterEach(() => {
	vi.unstubAllGlobals(); vi.clearAllMocks();
	fetchCorsiaAgent.mockResolvedValue({ kind: 'target', target: { thread_id: 'first-chat' } });
});

describe('setup model binding completion', () => {
	function catalog() {
		fetchCorsiaAgent.mockResolvedValue({ kind: 'target', target: { thread_id: 'first-chat' } });
		vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ models: [{
			id: 'codex/model', connection: 'codex', status: 'ready',
			runs_on: [{ selected: true, status: 'ready' }],
		}] }), { headers: { 'Content-Type': 'application/json' } })));
	}

	it.each([
		{ kind: 'pending', operation: { state: 'running' } },
		{ kind: 'committed', model: 'another/model' },
	])('keeps setup open until the intended model is committed: $kind', async (result) => {
		catalog(); save.mockResolvedValue(result);
		await expect(bindSetupFirstModel('disposable-session')).rejects.toMatchObject({ code: 'binding_incomplete' });
	});

	it.each([
		[422, 'catalog_refused'],
		[503, 'catalog_unavailable'],
	] as const)('codes catalog HTTP %i as %s', async (status, code) => {
		fetchCorsiaAgent.mockResolvedValue({ kind: 'target', target: { thread_id: 'first-chat' } });
		vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status })));
		await expect(bindSetupFirstModel('disposable-session')).rejects.toMatchObject({ code });
	});

	it('codes an unavailable agent without leaking lookup wording', async () => {
		fetchCorsiaAgent.mockResolvedValueOnce({ kind: 'not_found' });
		await expect(bindSetupFirstModel('disposable-session')).rejects.toMatchObject({ code: 'agent_unavailable' });
	});

	it('writes the handoff before binding and returns only the committed model', async () => {
		catalog(); const order: string[] = [];
		save.mockImplementation(async () => { order.push('bind'); return { kind: 'committed', model: 'codex/model' }; });
		await expect(bindSetupFirstModel('disposable-session', [], async () => { order.push('handoff'); }))
			.resolves.toEqual({ model: 'codex/model', source: 'codex' });
		expect(order).toEqual(['handoff', 'bind']);
	});
});
