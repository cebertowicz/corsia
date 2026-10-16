import { describe, expect, it } from 'vitest';
import type { ModelSource } from './conductorSources';
import { setupFinishNote, welcomeModelMeta } from './setupLocalPresentation';

const downloading = (fields: Partial<ModelSource>): ModelSource => ({
	source: 'omlx',
	name: 'Local model',
	state: 'needs_setup',
	setup_downloading: true,
	...fields,
});

describe('local setup presentation', () => {
	it('keeps the pick line free of a time estimate until transfer throughput is measured', () => {
		expect(welcomeModelMeta('Qwen 3.8', '30 GB', null, false))
			.toBe('Qwen 3.8 · 30 GB · runs in oMLX');
		expect(setupFinishNote(downloading({ download_eta_seconds: null }), true, ''))
			.toBe('Ready when the download finishes. You can close this window; CORSIA keeps going.');
	});

	it('adds only the measured ETA to the pick and wait lines', () => {
		expect(welcomeModelMeta('Qwen 3.8', '30 GB', 600, false))
			.toBe('Qwen 3.8 · 30 GB · runs in oMLX · about 10 min');
		expect(setupFinishNote(downloading({ download_eta_seconds: 600 }), true, ''))
			.toBe('Ready when the download finishes, about 10 min. You can close this window; CORSIA keeps going.');
	});

	it('represents a stalled download without an estimate', () => {
		expect(welcomeModelMeta('Qwen 3.8', '30 GB', 600, true))
			.toBe('Qwen 3.8 · 30 GB · runs in oMLX · Download paused');
		expect(setupFinishNote(downloading({ download_eta_seconds: 600, download_stalled: true }), true, ''))
			.toBe('Download paused. It will resume when progress returns.');
	});
});
