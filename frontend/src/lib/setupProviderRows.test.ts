import { describe, expect, it, vi } from 'vitest';

import { setupLocalRow, setupStartNote } from './setupProviderRows';

const actions = () => ({
	start: vi.fn(),
	stop: vi.fn(),
	stopOther: vi.fn(),
	check: vi.fn(),
});

describe('setup provider rows', () => {
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

	it.each([
		['connection', undefined, undefined, 'Couldn’t download the local model because the connection was lost. Check your internet connection, then Retry.'],
		['disk_full', undefined, undefined, 'Couldn’t download the local model: the disk is full. Free up 4 GB, then Retry.'],
		['memory', undefined, undefined, 'This computer doesn’t have enough memory to run the local model. Choose a subscription or API provider instead.'],
		['other_omlx', undefined, undefined, 'Another oMLX is running. Quit your other oMLX, then Retry.'],
		['other', undefined, 'setup-report-123', 'CORSIA couldn’t set up the local model. Retry. Reference: setup-report-123'],
		[undefined, 'setup_failed', undefined, 'CORSIA couldn’t set up the local model. Retry, or save a report.'],
		['other', 'setup_failed', undefined, 'CORSIA couldn’t set up the local model. Retry, or save a report.'],
		['connection', 'setup_failed', undefined, 'CORSIA couldn’t set up the local model. Retry, or save a report.'],
		['disk_full', 'setup_failed', undefined, 'CORSIA couldn’t set up the local model. Retry, or save a report.'],
		[undefined, 'start_failed', undefined, 'The local model downloaded but couldn’t start. Retry, or save a report.'],
	] as const)('gives %s/%s its own local setup recovery', (cause, outcome, reference, expected) => {
		const { start, ...otherActions } = actions();
		const row = setupLocalRow({
			...otherActions,
			start,
			source: { source: 'omlx', name: 'Local model', state: 'needs_setup', setup_error: 'failed', setup_error_code: cause, local_state: outcome, setup_error_reference: reference, bytes_to_free: 4_000_000_000 },
			pick: { state: 'picked', model: { name: 'Qwen 3.8', download_size_gb: 30 } } as never,
			eligible: true,
			downloading: false,
			ready: false,
			startError: '',
		});

		expect(row.meta).toBe(expected);
		expect(row.action?.label).toBe('Retry');
		row.action?.run();
		expect(start).toHaveBeenCalledOnce();
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
