import { describe, expect, it } from 'vitest';
import type { ModelSource } from './conductorSources';
import { setupFinishNote, setupSourceMeta, welcomeModelMeta, type PickedTools } from './setupLocalPresentation';

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
			.toBe('Ready when the download finishes in about 10 min. You can close this window; CORSIA keeps going.');
	});

	it('represents a stalled download without an estimate', () => {
		expect(welcomeModelMeta('Qwen 3.8', '30 GB', 600, true))
			.toBe('Qwen 3.8 · 30 GB · runs in oMLX · Download paused');
		expect(setupFinishNote(downloading({ download_eta_seconds: 600, download_stalled: true }), true, ''))
			.toBe('Download paused. It will resume when progress returns.');
	});

	it('says when their own oMLX is too old to check, with no Try again in the line', () => {
		const own = (cannotCheck: string | null) => downloading({
			setup_downloading: false, omlx_management: { state: 'managed', cannot_check: cannotCheck, required_version: '0.7.1rc1', restores: [], forgets_key: true } });
		expect(setupSourceMeta(own('0.6.2'), '', '30 GB'))
			.toBe('CORSIA couldn’t check which local AI model fits this computer: your oMLX is older than 0.7.1rc1. Updating oMLX lets CORSIA check.');
		expect(setupSourceMeta(own(null), '', '30 GB')).toBe('Free and private, and works offline. 30 GB download.');
		expect(setupSourceMeta(own(null), '', '')).toBe('Free and private, and works offline.');
		expect(setupSourceMeta(own('0.6.2'), 'failed', '30 GB')).toBe('Download needs attention');
	});
});

describe('the wait line, built from the rows shown', () => {
	const none: PickedTools = { waiting: [], idle: [], failed: [], localReady: false, keyOpen: false };
	const eta = downloading({ download_eta_seconds: 840 });

	it('leaves a cancelled row out of what it waits for', () => {
		expect(setupFinishNote(eta, true, '', { ...none, idle: ['Claude Code'] })).toBe('Ready when the download finishes in about 14 min. You can close this window; CORSIA keeps going.');
		expect(setupFinishNote(undefined, false, '', { ...none, idle: ['Claude Code'] })).toBe('Set up Claude Code to continue.');
	});

	it('counts an open key row as a route', () => {
		expect(setupFinishNote(eta, true, '', { ...none, keyOpen: true })).toBe('Ready when you save your key or the download finishes in about 14 min.');
		expect(setupFinishNote(undefined, false, '', { ...none, keyOpen: true })).toBe('Save your API key to continue.');
		expect(setupFinishNote(downloading({ download_stalled: true }), true, '', { ...none, keyOpen: true })).toBe('Ready when you save your key. The download is paused.');
		expect(setupFinishNote(undefined, false, '', { ...none, waiting: ['Claude Code'], keyOpen: true })).toBe('Ready when Claude Code is signed in or you save your key.');
	});

	it('says the download the same way in all three shapes, with a measured time only', () => {
		const clause = 'the download finishes in about 14 min';
		expect(setupFinishNote(eta, true, '', { ...none, waiting: ['Claude Code'] })).toBe(`Ready when Claude Code is signed in or ${clause}.`);
		expect(setupFinishNote(eta, true, '', { ...none, waiting: ['Claude Code'], keyOpen: true })).toBe(`Ready when Claude Code is signed in, you save your key or ${clause}.`);
		expect(setupFinishNote(eta, true, '', { ...none, keyOpen: true })).toBe(`Ready when you save your key or ${clause}.`);
		expect(setupFinishNote(eta, true, '', none)).toContain(`Ready when ${clause}.`);
		const noEta = downloading({ download_eta_seconds: null });
		expect(setupFinishNote(noEta, true, '', { ...none, waiting: ['Claude Code'] })).toBe('Ready when Claude Code is signed in or the download finishes.');
		expect(setupFinishNote(noEta, true, '', { ...none, keyOpen: true })).toBe('Ready when you save your key or the download finishes.');
		expect(setupFinishNote(noEta, true, '')).toContain('Ready when the download finishes.');
	});

	it('names no tool the screen does not show', () => {
		expect(setupFinishNote(undefined, false, '', { ...none, keyOpen: true })).not.toMatch(/Claude|Codex/);
		expect(setupFinishNote(undefined, false, '')).toBe('Set up Claude Code or Codex to continue.');
	});

	it('says chat runs on the local model for now after a failure, and asks for Try again when it cannot', () => {
		expect(setupFinishNote(undefined, false, '', { ...none, failed: ['Claude Code'], localReady: true })).toBe('Chat runs on the local model for now.');
		expect(setupFinishNote(undefined, false, '', { ...none, failed: ['Claude Code'] })).toBe('Choose Try again on Claude Code to continue.');
	});
});
