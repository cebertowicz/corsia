import { describe, it, expect, beforeEach, vi } from 'vitest';

const captureSpy = vi.fn();

vi.mock('posthog-js', () => ({
	default: {
		init: vi.fn(),
		capture: (...args: unknown[]) => captureSpy(...args),
		identify: vi.fn(),
	},
}));

import { emitProductEvent, _resetForTests } from '../../product_events';

function stubEnv(): void {
	(import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_HOST = 'https://analytics.example.test';
	(import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_API_KEY = 'test-api-key';
}

function clearEnv(): void {
	delete (import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_API_KEY;
	delete (import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_HOST;
	delete (import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_PROJECT;
}

beforeEach(() => {
	captureSpy.mockReset();
	_resetForTests();
	clearEnv();
	stubEnv();
});

describe('composer.sent contract', () => {
	it('captures char_count + attachment_count + thread_id', () => {
		emitProductEvent('composer', 'sent', {
			thread_id: 'sess-abc',
			char_count: 42,
			attachment_count: 1,
		});
		expect(captureSpy).toHaveBeenCalledTimes(1);
		expect(captureSpy).toHaveBeenCalledWith(
			'composer.sent',
			expect.objectContaining({
				char_count: 42,
				attachment_count: 1,
				thread_id: 'sess-abc',
				project: 'corsia',
			}),
		);
	});
});

describe('composer.attached + attachment.added contract', () => {
	it('emits composer.attached with file_count', () => {
		emitProductEvent('composer', 'attached', {
			thread_id: 'sess-xyz',
			file_count: 3,
		});
		expect(captureSpy).toHaveBeenCalledWith(
			'composer.attached',
			expect.objectContaining({ file_count: 3, thread_id: 'sess-xyz' }),
		);
	});

	it('emits attachment.added with mime_type + is_image', () => {
		emitProductEvent('attachment', 'added', {
			thread_id: 'sess-xyz',
			mime_type: 'image/png',
			is_image: true,
		});
		expect(captureSpy).toHaveBeenCalledWith(
			'attachment.added',
			expect.objectContaining({ mime_type: 'image/png', is_image: true }),
		);
	});
});

describe('model.switched contract', () => {
	it('mirrors the backend taxonomy: provider + model_id + reasoning_shape + previous_model_id', () => {
		emitProductEvent('model', 'switched', {
			thread_id: 'sess-1',
			model_id: 'claude-opus-4-7',
			provider: 'anthropic',
			previous_model_id: 'claude-sonnet-4-5',
			reasoning_shape: 'effort_levels',
		});
		expect(captureSpy).toHaveBeenCalledWith(
			'model.switched',
			expect.objectContaining({
				model_id: 'claude-opus-4-7',
				provider: 'anthropic',
				reasoning_shape: 'effort_levels',
				previous_model_id: 'claude-sonnet-4-5',
			}),
		);
	});
});

describe('ui.* contract', () => {
	it('ui.tab_switched carries from + to', () => {
		emitProductEvent('ui', 'tab_switched', { from: 'chat', to: 'settings' });
		expect(captureSpy).toHaveBeenCalledWith(
			'ui.tab_switched',
			expect.objectContaining({ from: 'chat', to: 'settings' }),
		);
	});

	it('ui.modal_opened carries modal_name', () => {
		emitProductEvent('ui', 'modal_opened', { modal_name: 'lightbox' });
		expect(captureSpy).toHaveBeenCalledWith(
			'ui.modal_opened',
			expect.objectContaining({ modal_name: 'lightbox' }),
		);
	});

	it('ui.settings_opened emits with empty properties', () => {
		emitProductEvent('ui', 'settings_opened', {});
		expect(captureSpy).toHaveBeenCalledWith(
			'ui.settings_opened',
			expect.objectContaining({ project: 'corsia' }),
		);
	});

	it('ui.thinking_toggled carries effort + reasoning_shape', () => {
		emitProductEvent('ui', 'thinking_toggled', {
			thread_id: 'sess-q',
			effort: 'high',
			reasoning_shape: 'effort_levels',
		});
		expect(captureSpy).toHaveBeenCalledWith(
			'ui.thinking_toggled',
			expect.objectContaining({ effort: 'high', reasoning_shape: 'effort_levels' }),
		);
	});
});

describe('event-name shape is taxonomy-compliant', () => {
	it('every wired event name is lowercase, dot-separated, no spaces', () => {
		const wired = [
			['composer', 'sent'],
			['composer', 'attached'],
			['attachment', 'added'],
			['model', 'switched'],
			['ui', 'tab_switched'],
			['ui', 'modal_opened'],
			['ui', 'settings_opened'],
			['ui', 'thinking_toggled'],
		];
		for (const [cat, action] of wired) {
			expect(`${cat}.${action}`).toMatch(/^[a-z]+\.[a-z_]+$/);
		}
	});
});
