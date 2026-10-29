import { describe, expect, it, vi } from 'vitest';

import { setupLocalRow, setupStartNote } from './setupProviderRows';

const actions = () => ({
	start: vi.fn(),
	stop: vi.fn(),
	check: vi.fn(),
});

// Mirrors the emitted projection: one local state, cause, recovery line and optional reference.
const flightShapes = [
	{ state: 'failed', cause: 'connection', fix: 'Check your internet connection, then try again.' },
	{ state: 'failed', cause: 'disk_full', bytes_to_free: 4_000_000_000, fix: 'The local model needs 4.0 GB more free space. Free it up, then try again.' },
	{ state: 'failed', cause: 'memory', fix: 'Choose a subscription or API provider instead.' },
	{ state: 'failed', cause: 'other', ref: 'setup-report-123', fix: 'CORSIA couldn’t set up the local model.' },
	{ state: 'setup_failed', cause: 'other', ref: 'setup-report-456', fix: 'CORSIA couldn’t set up the local model.' },
	{ state: 'start_failed', cause: 'connection', ref: 'setup-report-789', fix: 'Check your internet connection, then try again.' },
	{ state: 'setup_failed', cause: 'own_omlx_upgrade_required', ref: 'setup-report-999', fix: 'The oMLX app already on this computer is out of date or broken. Update it, then press Retry.' }
] as const;

function sourceFromFlight(flight: typeof flightShapes[number]) {
	return {
		source: 'omlx', name: 'Local model', state: 'needs_setup' as const,
		local_state: flight.state, setup_error_code: flight.cause,
		setup_error_reference: 'ref' in flight ? flight.ref : undefined,
		bytes_to_free: 'bytes_to_free' in flight ? flight.bytes_to_free : undefined,
		fix: flight.fix,
	};
}

describe('setup provider rows', () => {
	it('tells a computer below the memory floor why and what to do instead', () => {
		const row = setupLocalRow({ ...actions(), pick: null, eligible: false, downloading: false, ready: false, startError: '' });

		expect(row.meta).toBe('Local models need at least 32 GB of memory, and this computer has less. Smaller models and future updates may bring them to computers with less memory. Connect a subscription or an API provider.');
	});

	it('starts an installed stopped model below the setup floor without a pick', () => {
		const start = vi.fn();
		const row = setupLocalRow({ ...actions(), start, source: { source: 'omlx', name: 'Local model', state: 'needs_setup', reason_code: 'not_running', local_state: 'stopped' }, pick: null, eligible: false, downloading: false, ready: false, startError: '' });
		expect(row.meta).toBe('Local model · Off');
		row.toggle?.run(true);
		expect(start).toHaveBeenCalledOnce();
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

	it('shows an existing ready model without a memory refusal below the floor', () => {
		const row = setupLocalRow({ ...actions(), pick: null, eligible: false, downloading: false, ready: true, startError: '' });

		expect(row).toMatchObject({ meta: 'Local model · runs on this computer', pill: { state: 'success', label: 'Ready' } });
		expect(row.meta).not.toContain('Local models need at least 32 GB of memory');
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

		expect(row.meta).toBe(flight.fix);
		expect(row.reference).toBe('ref' in flight ? flight.ref : undefined);
		expect(row.action?.label).toBe('Retry');
		row.action?.run();
		expect(start).toHaveBeenCalledOnce();
	});

	it('keeps a network failure reference and offers its report action', () => {
		const row = setupLocalRow({ ...actions(), pick: null, eligible: true, downloading: false, ready: false, startError: 'other', startFix: 'CORSIA couldn’t start local setup.', startReference: 'local-request-123' });

		expect(row).toMatchObject({ meta: 'CORSIA couldn’t start local setup.', reference: 'local-request-123', reportable: true });
	});

	it('shows the actionable oMLX update when preparing a pick fails for that cause', () => {
		const row = setupLocalRow({ ...actions(), pick: { state: 'failed', model: null, message: null, cause: 'own_omlx_upgrade_required', ref: 'pick-123' }, eligible: true, downloading: false, ready: false, startError: '' });

		expect(row).toMatchObject({ meta: 'The oMLX app already on this computer is out of date or broken. Update it, then press Retry.', reference: 'pick-123', reportable: true, action: { label: 'Retry' } });
	});

	it.each(['other', 'connection', 'disk_full', undefined])('keeps the pick failure reference for %s without claiming a fit failure', (cause) => {
		const check = vi.fn();
		const row = setupLocalRow({ ...actions(), check, pick: { state: 'failed', model: null, message: null, cause, ref: 'local-123' }, eligible: true, downloading: false, ready: false, startError: '' });
		expect(row.meta).toBe('CORSIA couldn’t set up the local model.');
		expect(row.reference).toBe('local-123');
		expect(row.reportable).toBe(true);
		row.action?.run();
		expect(check).toHaveBeenCalledOnce();
	});

	it('names a model fit failure only when memory is its cause, retaining the reference', () => {
		const row = setupLocalRow({ ...actions(), pick: { state: 'failed', model: null, message: null, cause: 'memory', ref: 'local-456' }, eligible: true, downloading: false, ready: false, startError: '' });
		expect(row.meta).toBe('Couldn’t find a local model that fits this computer. Try again.');
		expect(row.reference).toBe('local-456');
	});

	it('explains the gate from the current working and download states', () => {
		expect(setupStartNote(false, false, false))
			.toBe('Set up a subscription or add an API provider to continue.');
		expect(setupStartNote(false, true, true, 720))
			.toBe('Ready when the local model finishes downloading (12 min left).');
		expect(setupStartNote(true, true, true))
			.toBe('The local model keeps downloading. Follow it in Settings › Providers.');
	});
});

describe('setup local row switch', () => {
	const downloadingSource = { source: 'omlx', name: 'Local model', state: 'needs_setup' as const, local_state: 'downloading', download_percent: 50, download_size: '10 GB' };
	const downloadingRow = (extra: Partial<Parameters<typeof setupLocalRow>[0]> = {}) => setupLocalRow({ ...actions(), source: downloadingSource, pick: null, eligible: true, downloading: true, ready: false, startError: '', ...extra });

	it('never offers Download again once the download finished and the model is still being listed', () => {
		const row = setupLocalRow({ ...actions(), source: { source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'ready' }, pick: null, eligible: true, downloading: false, ready: false, startError: '' });
		expect(row.action?.label).toBe('Check again');
		expect(row.busy).toBe(true);
		expect(row.spinning).toBe(true);
		expect(row.meta).toContain('Loading…');
		expect(row.meta).not.toContain('Finishing setup');
	});

	it('shows a turned-off local model as off, and its switch turns it back on', () => {
		const start = vi.fn();
		const row = setupLocalRow({ ...actions(), start, source: { source: 'omlx', name: 'Local model', state: 'needs_setup', reason_code: 'not_running', local_state: 'stopped' }, pick: null, eligible: true, downloading: false, ready: false, startError: '', cloudReady: true });
		expect(row).toMatchObject({ meta: 'Local model · Off', toggle: { on: false } });
		expect(row.busy).toBeUndefined();
		expect(row.action).toBeUndefined();
		row.toggle?.run(true);
		expect(start).toHaveBeenCalledOnce();
	});

	it('keeps a failed turn-off on the Ready row, with its reference and a report', () => {
		const row = setupLocalRow({ ...actions(), source: undefined, pick: null, eligible: true, downloading: false, ready: true, startError: '', cloudReady: true, turnOffFailed: { reference: 'local-0123456789abcdef' } });
		expect(row).toMatchObject({ error: true, reportable: true, reference: 'local-0123456789abcdef', toggle: { on: true, held: false } });
		expect(row.detail).toBe('CORSIA couldn’t turn off the local model. Turn it off again, or save a report.');
	});

	it('holds the switch on while the local model is the only way through setup', () => {
		const row = downloadingRow();
		expect(row.action).toBeUndefined();
		expect(row.toggle).toMatchObject({ on: true, held: true });
		expect(row.detail).toBe('CORSIA is downloading the local model so setup can continue. To turn it off, sign in to a subscription (Back to Your subscriptions) or add an API provider first.');
	});

	it('asks before deleting a download once a cloud provider is ready', () => {
		const confirmStop = vi.fn(), stop = vi.fn();
		const row = downloadingRow({ cloudReady: true, confirmStop, stop });
		expect(row.toggle).toMatchObject({ on: true, held: false });
		row.toggle!.run(false);
		expect(confirmStop).toHaveBeenCalledWith(true);
		expect(stop).not.toHaveBeenCalled();

		const asking = downloadingRow({ cloudReady: true, confirmingStop: true, confirmStop, stop });
		expect(asking.detail).toBe('Stop downloading the local model? The 5 GB downloaded so far is deleted.');
		expect(asking.confirm?.map((item) => item.label)).toEqual(['Stop download', 'Keep downloading']);
		asking.confirm![1].run();
		expect(stop).not.toHaveBeenCalled();
		asking.confirm![0].run();
		expect(stop).toHaveBeenCalledOnce();
	});

	it('turns a ready local model off through the local server only once a cloud provider is ready', () => {
		const turnOff = vi.fn();
		const held = setupLocalRow({ ...actions(), pick: null, eligible: true, downloading: false, ready: true, startError: '', turnOff });
		expect(held.toggle).toMatchObject({ on: true, held: true });
		const free = setupLocalRow({ ...actions(), pick: null, eligible: true, downloading: false, ready: true, startError: '', cloudReady: true, turnOff });
		free.toggle!.run(false);
		expect(turnOff).toHaveBeenCalledOnce();
	});
});
