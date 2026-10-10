import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/settings', () => ({
	loadModelProfile: vi.fn(),
	updateModelProfile: vi.fn(),
}));

import ModelDetailsModal from './ModelDetailsModal.svelte';
import { loadModelProfile, updateModelProfile } from '$lib/stores/settings';

async function settle() {
	for (let i = 0; i < 5; i++) {
		await Promise.resolve();
		await tick();
	}
	flushSync();
}

function model(id: string) {
	return {
		id,
		name: id,
		state: 'default' as const,
		meta: '1M context · checkpoints at 262k',
		caps: [],
		dbId: id,
		providerContextLength: 1_000_000,
	};
}

describe('ModelDetailsModal checkpointing', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		vi.clearAllMocks();
	});

	it('saves a checkpoint window for a harness model', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({ context_window_override: 262_144 } as never);
		vi.mocked(updateModelProfile).mockResolvedValue(true);
		component = mount(ModelDetailsModal, {
			target: document.body,
			props: {
				model: model('codex/gpt-5.6-luna'), providerLabel: 'Codex', providerName: 'codex',
				labelEditable: true, onClose: () => {},
			},
		});
		await settle();

		expect(document.body.textContent).toContain('Checkpoint context window (tokens)');
		const input = document.querySelector('input[inputmode="numeric"]') as HTMLInputElement;
		input.value = '131072';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		[...document.querySelectorAll('button')].filter((button) => button.textContent?.includes('Save')).at(-1)?.click();
		await settle();
		expect(updateModelProfile).toHaveBeenCalledWith('codex/gpt-5.6-luna', { context_window: 131072 });
	});

	it('shows provider context and checkpoint window as distinct facts for a harness model', async () => {
		component = mount(ModelDetailsModal, {
			target: document.body,
			props: {
				model: { ...model('codex/gpt-5.6-luna'), contextLength: 262_144 },
				providerLabel: 'Codex', providerName: 'codex', labelEditable: true, onClose: () => {},
			},
		});
		await settle();

		expect(document.body.textContent).toContain('Context1,000,000 tokens');
		expect(document.body.textContent).toContain('Checkpoint window262,144 tokens');
	});

	it('shows the checkpoint editor and oMLX administration link without sampling fields', async () => {
		vi.mocked(loadModelProfile).mockResolvedValue({ context_window_override: 262_144 } as never);
		component = mount(ModelDetailsModal, {
			target: document.body,
			props: {
				model: model('omlx@srv-test/Qwen3.8-Flash-Next'), providerLabel: 'oMLX', providerName: 'omlx',
				omlxAdminUrl: 'http://127.0.0.1:5100/admin', onClose: () => {},
			},
		});
		await settle();

		expect(document.body.textContent).toContain('Checkpoint context window (tokens)');
		expect(document.querySelector('a')?.textContent).toBe('Sampling is set in oMLX');
		expect(document.querySelector('a')?.getAttribute('href')).toBe('http://127.0.0.1:5100/admin');
		expect(document.body.textContent).not.toContain('Temperature');
	});
});
