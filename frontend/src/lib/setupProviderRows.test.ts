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

	it('keeps one cause-specific retry action for a failed local download', () => {
		const { start, ...otherActions } = actions();
		const row = setupLocalRow({
			...otherActions,
			start,
			source: { source: 'omlx', name: 'Local model', state: 'needs_setup', setup_error: 'failed', setup_error_code: 'disk_full', bytes_to_free: 4_000_000_000 },
			pick: { state: 'picked', model: { name: 'Qwen 3.8', download_size_gb: 30 } } as never,
			eligible: true,
			downloading: false,
			ready: false,
			startError: '',
		});

		expect(row.meta).toContain('the disk is full');
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
