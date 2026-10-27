import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/settings', () => ({
	loadModelProfile: vi.fn(),
	updateModelProfile: vi.fn(),
}));

import CheckpointWindowEditor from './CheckpointWindowEditor.svelte';
import { loadModelProfile, updateModelProfile } from '$lib/stores/settings';

async function settle() {
	for (let i = 0; i < 5; i++) {
		await Promise.resolve();
		await tick();
	}
	flushSync();
}

function type(value: string) {
	const input = document.querySelector('input') as HTMLInputElement;
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

function saveButton() {
	return [...document.querySelectorAll('button')].find((b) => b.textContent?.includes('Save'));
}

describe('CheckpointWindowEditor', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		vi.clearAllMocks();
	});

	it('labels the window as a checkpoint setting and saves only the context window', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({ context_window: null, context_window_override: null } as never);
		vi.mocked(updateModelProfile).mockResolvedValue(true);
		const onSaved = vi.fn();
		component = mount(CheckpointWindowEditor, { target: document.body, props: { dbId: 'ext-1', onSaved } });
		await settle();

		expect(document.body.textContent).toContain('Checkpoint context window (tokens)');
		expect(document.body.textContent).toContain('never sent to the provider');
		expect(document.body.textContent).toContain('The provider reports no context window, so set one to use this model. CORSIA checkpoints a chat before it outgrows it.');

		type('1000000');
		saveButton()?.click();
		await settle();

		expect(updateModelProfile).toHaveBeenCalledWith('ext-1', { context_window: 1000000 });
		expect(onSaved).toHaveBeenCalled();
	});

	it('shows only the saved override, never the discovered window, and names the provider value', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({ context_window: 997952, context_window_override: null } as never);
		component = mount(CheckpointWindowEditor, {
			target: document.body,
			props: { dbId: 'ext-1', providerWindow: 997952 },
		});
		await settle();

		expect((document.querySelector('input') as HTMLInputElement).value).toBe('');
		expect(document.body.textContent).toContain('Leave empty to use the provider');
		expect(saveButton()?.disabled).toBe(true);
	});

	it('clears a saved window so the provider window applies again', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({ context_window: 1000000, context_window_override: 1000000 } as never);
		vi.mocked(updateModelProfile).mockResolvedValue(true);
		component = mount(CheckpointWindowEditor, {
			target: document.body,
			props: { dbId: 'ext-1', providerWindow: 997952 },
		});
		await settle();

		type('');
		expect(saveButton()?.disabled).toBe(false);
		saveButton()?.click();
		await settle();

		expect(updateModelProfile).toHaveBeenCalledWith('ext-1', { context_window: null });
	});

	it('names a saved window as saved and keeps the provider window visible', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({
			context_window: 500_000,
			context_window_override: 500_000,
			context_window_source: 'saved',
			context_window_unsaved: 262_144,
			context_window_unsaved_source: 'default',
		} as never);
		component = mount(CheckpointWindowEditor, {
			target: document.body,
			props: { dbId: 'ext-1', providerWindow: 1_000_000 },
		});
		await settle();

		expect((document.querySelector('input') as HTMLInputElement).value).toBe('500000');
		expect(document.body.textContent).toContain('Current: 500,000 tokens · Saved');
		expect(document.body.textContent).toContain('Saved');
		expect(document.body.textContent).toContain('Provider window: 1,000,000');
		// Clearing lands on the CORSIA default, not the provider's window.
		expect((document.querySelector('input') as HTMLInputElement).placeholder).toBe('262144');
		expect(document.body.textContent).toContain('Leave empty for the CORSIA default, 262,144 tokens.');
	});

	it('says a cleared saved window returns to the provider window when that is the unsaved one', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({
			context_window: 100_000,
			context_window_override: 100_000,
			context_window_source: 'saved',
			context_window_unsaved: 131_072,
			context_window_unsaved_source: 'provider',
		} as never);
		component = mount(CheckpointWindowEditor, {
			target: document.body,
			props: { dbId: 'ext-1', providerWindow: 131_072 },
		});
		await settle();

		expect((document.querySelector('input') as HTMLInputElement).placeholder).toBe('131072');
		expect(document.body.textContent).toContain("Leave empty to use the provider's 131,072 tokens.");
	});

	it('shows the CORSIA default as the current window with the provider above it', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({
			context_window: 262_144,
			context_window_override: null,
			context_window_source: 'default',
			context_window_unsaved: 262_144,
			context_window_unsaved_source: 'default',
		} as never);
		component = mount(CheckpointWindowEditor, {
			target: document.body,
			props: { dbId: 'ext-1', providerWindow: 1_000_000 },
		});
		await settle();

		expect(document.body.textContent).toContain('Current: 262,144 tokens · CORSIA default');
		expect(document.body.textContent).toContain('Leave empty for the CORSIA default, 262,144 tokens. Provider window: 1,000,000.');
		// Nothing is saved, so there is nothing to clear and nothing to save.
		expect((document.querySelector('input') as HTMLInputElement).value).toBe('');
		expect(saveButton()?.disabled).toBe(true);
	});

	it('refuses a non-integer window', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({ context_window: null, context_window_override: null } as never);
		component = mount(CheckpointWindowEditor, { target: document.body, props: { dbId: 'ext-1' } });
		await settle();

		type('12.5');

		expect(document.body.textContent).toContain('Enter a whole number of tokens.');
		expect(saveButton()?.disabled).toBe(true);
	});

	it('reports a failed load instead of offering an empty required field', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue(null);
		component = mount(CheckpointWindowEditor, { target: document.body, props: { dbId: 'ext-1' } });
		await settle();

		expect(document.body.textContent).toContain('Could not load the saved checkpoint window.');
		expect(document.querySelector('input')).toBeNull();
	});
});
