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
	{ state: 'start_failed', cause: 'load_refused', ref: 'local-42', fix: 'CORSIA couldn’t load the model because there isn’t enough free memory. Close other apps, then press Retry.', detail: 'Not loaded: needs 12 GB, 8 GB free for a model now' },
	{ state: 'setup_failed', cause: 'own_omlx_upgrade_required', ref: 'setup-report-999', fix: 'The oMLX app already on this computer is out of date or broken. Update it, then press Retry.' }
] as const;

function sourceFromFlight(flight: typeof flightShapes[number]) {
	return {
		source: 'omlx', name: 'Local model', state: 'needs_setup' as const,
		local_state: flight.state, setup_error_code: flight.cause,
		setup_error_reference: 'ref' in flight ? flight.ref : undefined,
		bytes_to_free: 'bytes_to_free' in flight ? flight.bytes_to_free : undefined,
		fix: flight.fix,
		detail: 'detail' in flight ? flight.detail : undefined,
	};
}

describe('setup provider rows', () => {
	it('tells a computer under 24 GB why and what to do instead', () => {
		const row = setupLocalRow({ ...actions(), pick: null, eligible: false, downloading: false, ready: false, startError: '' });

		expect(row.meta).toBe('Adding a local model through CORSIA needs at least 32 GB of memory. Smaller models and future updates may bring local models to computers with less memory. Connect a subscription or an API provider.');
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

	it('keeps an already ready local model usable below the memory floor', () => {
		const row = setupLocalRow({ ...actions(), pick: { state: 'low_memory', model: null, message: null }, eligible: false, downloading: false, ready: true, startError: '' });
		expect(row.pill).toEqual({ state: 'success', label: 'Ready' });
		expect(row.toggle).toMatchObject({ on: true, held: true });
		expect(row.action).toBeUndefined();
		expect(row.detail).toContain('needs the local model to continue setup');
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
		expect(row.detail).toBe('detail' in flight ? flight.detail : undefined);
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
		expect(setupStartNote(false, false, 'idle'))
			.toBe('Set up a subscription or add an API provider to continue.');
		expect(setupStartNote(false, true, 'downloading', 720))
			.toBe('Ready when the local model finishes downloading (12 min left).');
		expect(setupStartNote(true, true, 'downloading'))
			.toBe('The local model keeps downloading. Follow it in Settings › Providers.');
		expect(setupStartNote(false, true, 'idle'))
			.toBe('Download the local model, set up a subscription or add an API provider to continue.');
	});

	const local = (localState?: string, extra: Partial<Parameters<typeof setupLocalRow>[0]> = {}) => {
		const downloading = ['downloading', 'paused', 'starting'].includes(localState ?? '');
		const row = setupLocalRow({ ...actions(), source: localState ? { source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: localState } : undefined, pick: null, eligible: true, downloading, ready: false, startError: '', ...extra });
		return { row, phase: row.phase };
	};

	it('says the local model is loading, in the glossary word, while its row shows it starting or loading', () => {
		for (const { row, phase } of [local('starting'), local('ready'), local('ready', { startError: 'other' }), local('starting', { startError: 'other' })]) {
			expect(row.busy).toBe(true);
			expect(phase).toBe('loading');
			expect(setupStartNote(false, true, phase)).toBe('Ready when the local model finishes loading.');
			expect(setupStartNote(true, true, phase)).toBe('');
		}
	});

	it('names the download while the local model is getting ready to download', () => {
		const { row, phase } = local(undefined, { preparing: true });
		expect(row.meta).toBe('Getting the local model ready…');
		expect(setupStartNote(false, true, phase)).toBe('Ready when the local model finishes downloading.');
	});

	it('offers the switch in the footer when a downloaded local model is off', () => {
		const { row, phase } = local(undefined, { source: { source: 'omlx', name: 'Local model', state: 'needs_setup', reason_code: 'not_running', local_state: 'stopped' } });
		expect(row.toggle?.on).toBe(false);
		expect(setupStartNote(false, true, phase)).toBe('Turn on the local model, set up a subscription or add an API provider to continue.');
	});

	it('says the local model keeps downloading only while it downloads or is paused', () => {
		expect(local('downloading').phase).toBe('downloading');
		expect(local('paused').phase).toBe('downloading');
		for (const state of ['starting', 'ready', 'stopped', undefined]) expect(setupStartNote(true, true, local(state).phase)).toBe('');
	});

	it('points the footer at Retry only when the row shows Retry', () => {
		const pickFailed = { state: 'failed' as const, model: null, message: null, cause: 'memory', ref: 'local-1' };
		for (const { row, phase } of [local('failed'), local('setup_failed'), local('start_failed'), local(undefined, { startError: 'other' }), local(undefined, { pick: pickFailed })]) {
			expect(row.action?.label).toBe('Retry');
			expect(phase).toBe('failed');
			expect(setupStartNote(false, true, phase)).toBe('Press Retry, set up a subscription or add an API provider to continue.');
		}
		expect(setupStartNote(false, false, 'failed')).toBe('Set up a subscription or add an API provider to continue.');
	});

	it('offers Download in the footer only when the row offers it', () => {
		expect(local(undefined).phase).toBe('idle');
		const full = local(undefined, { pick: { state: 'picked', model: { name: 'M', repository: 'r', download_size_bytes: 1, download_size_gb: 10, context_window: 1 }, message: null }, freeDisk: 2 });
		expect(full.row.action?.label).toBe('Check again');
		expect(setupStartNote(false, true, full.phase)).toBe('Set up a subscription or add an API provider to continue.');
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
