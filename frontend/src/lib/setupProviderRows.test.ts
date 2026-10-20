import { describe, expect, it, vi } from 'vitest';

import { setupLocalRow, setupStartNote } from './setupProviderRows';

const actions = () => ({
	start: vi.fn(),
	stop: vi.fn(),
	check: vi.fn(),
});

// Matches the four fields the signed-in source row receives from local-download.
const flightShapes = [
	{ state: 'failed', cause: 'connection', expected: 'Couldn’t download the local model because the connection was lost. Check your internet connection, then Retry.' },
	{ state: 'failed', cause: 'disk_full', bytes_to_free: 4_000_000_000, expected: 'Couldn’t download the local model: the disk is full. Free up 4 GB, then Retry.' },
	{ state: 'failed', cause: 'memory', expected: 'This computer doesn’t have enough memory to run the local model. Choose a subscription or API provider instead.' },
	{ state: 'failed', cause: 'other', ref: 'setup-report-123', expected: 'CORSIA couldn’t set up the local model. Retry. Reference: setup-report-123' },
	{ state: 'failed', cause: undefined, ref: 'setup-report-234', expected: 'CORSIA couldn’t set up the local model. Retry. Reference: setup-report-234' },
	{ state: 'setup_failed', cause: 'other', ref: 'setup-report-456', expected: 'CORSIA couldn’t set up the local model. Retry, or save a report. Reference: setup-report-456' },
	{ state: 'start_failed', cause: 'connection', ref: 'setup-report-789', expected: 'The local model downloaded but couldn’t start. Retry, or save a report. Reference: setup-report-789' },
	{ state: 'setup_failed', cause: 'own_omlx_upgrade_required', ref: 'setup-report-999', expected: 'Update your oMLX, then Retry. Reference: setup-report-999' }
] as const;

function sourceFromFlight(flight: typeof flightShapes[number]) {
	return {
		source: 'omlx', name: 'Local model', state: 'needs_setup' as const, setup_error: 'failed',
		local_state: flight.state, setup_error_code: flight.cause,
		setup_error_reference: 'ref' in flight ? flight.ref : undefined,
		bytes_to_free: 'bytes_to_free' in flight ? flight.bytes_to_free : undefined,
	};
}

describe('setup provider rows', () => {
	it('tells a computer under 24 GB why, what to do instead, and that smaller local models may come', () => {
		const row = setupLocalRow({ ...actions(), pick: null, eligible: false, downloading: false, ready: false, startError: '' });

		expect(row.meta).toBe('Local models need at least 24 GB of memory, and this computer has less. Connect a subscription or an API provider. Smaller local models may come in a future update.');
	});

	it('shows a ready local model as a completed provider row', () => {
		const row = setupLocalRow({
			...actions(),
			pick: { state: 'picked', model: { name: 'Qwen 3.8', download_size_gb: 30 } } as never,
			eligible: true,
			downloading: false,
			ready: true,
			startError: '',
		});

		expect(row).toMatchObject({
			key: 'omlx',
			meta: 'Qwen 3.8 · runs on this computer',
			pill: { state: 'success', label: 'Ready' },
		});
	});

	it.each(flightShapes)('gives $state/$cause its matching local setup recovery', (flight) => {
		const { start, ...otherActions } = actions();
		const row = setupLocalRow({
			...otherActions,
			start,
			source: sourceFromFlight(flight),
			pick: { state: 'picked', model: { name: 'Qwen 3.8', download_size_gb: 30 } } as never,
			eligible: true,
			downloading: false,
			ready: false,
			startError: '',
		});

		expect(row.meta).toBe(flight.expected);
		expect(row.action?.label).toBe('Retry');
		row.action?.run();
		expect(start).toHaveBeenCalledOnce();
	});

	it('keeps a request failure reference and offers its report action', () => {
		const row = setupLocalRow({ ...actions(), pick: null, eligible: true, downloading: false, ready: false, startError: 'other', startReference: 'local-request-123' });

		expect(row).toMatchObject({ meta: 'CORSIA couldn’t set up the local model. Retry. Reference: local-request-123', reportable: true });
	});

	it('shows the actionable oMLX update when preparing a pick fails for that cause', () => {
		const row = setupLocalRow({ ...actions(), pick: { state: 'failed', model: null, message: null, cause: 'own_omlx_upgrade_required', ref: 'pick-123' }, eligible: true, downloading: false, ready: false, startError: '' });

		expect(row).toMatchObject({ meta: 'Update your oMLX, then Retry. Reference: pick-123', reportable: true, action: { label: 'Retry' } });
	});

	it.each(['other', 'connection', 'disk_full', undefined])('keeps the pick failure reference for %s without claiming a fit failure', (cause) => {
		const check = vi.fn();
		const row = setupLocalRow({ ...actions(), check, pick: { state: 'failed', model: null, message: null, cause, ref: 'local-123' }, eligible: true, downloading: false, ready: false, startError: '' });
		expect(row.meta).toBe('CORSIA couldn’t choose a local model. Retry, or save a report. Reference: local-123');
		expect(row.reportable).toBe(true);
		row.action?.run();
		expect(check).toHaveBeenCalledOnce();
	});

	it('names a model fit failure only when memory is its cause, retaining the reference', () => {
		const row = setupLocalRow({ ...actions(), pick: { state: 'failed', model: null, message: null, cause: 'memory', ref: 'local-456' }, eligible: true, downloading: false, ready: false, startError: '' });
		expect(row.meta).toBe('Couldn’t find a local model that fits this computer. Retry. Reference: local-456');
	});

	it('explains the gate from the current working and download states', () => {
		expect(setupStartNote(false, false, false))
			.toBe('Set up a subscription or add an API provider to continue.');
		expect(setupStartNote(false, true, true, 720))
			.toBe('Ready when the local model finishes downloading, in about 12 min.');
		expect(setupStartNote(true, true, true))
			.toBe('The local model keeps downloading. Follow it in Settings › Providers.');
	});
});
