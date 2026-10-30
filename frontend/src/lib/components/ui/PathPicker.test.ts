import { mount, unmount } from 'svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import PathPicker from './PathPicker.svelte';
import { token } from '$lib/stores/auth';
import { isProjectRootChoice } from '$lib/projectRoot';

const ROOT = '/workspace/projects';

const TREE: Record<string, { parent: string | null; dirs: { name: string; is_git: boolean; is_link?: boolean }[] }> = {
	[ROOT]: {
		parent: '/workspace',
		dirs: [
			{ name: 'alpha', is_git: false, is_link: false },
			{ name: 'linked', is_git: false, is_link: true },
		],
	},
	[`${ROOT}/alpha`]: { parent: ROOT, dirs: [{ name: 'source', is_git: false, is_link: false }] },
	[`${ROOT}/alpha/source`]: { parent: `${ROOT}/alpha`, dirs: [] },
	'/workspace': { parent: null, dirs: [{ name: 'projects', is_git: false }] },
};

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function chooseButton(): HTMLButtonElement {
	const found = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
		b.textContent?.includes('Choose this folder'),
	);
	if (!found) throw new Error('no choose button');
	return found;
}

function rowButton(name: string): HTMLButtonElement {
	const found = [...document.body.querySelectorAll<HTMLButtonElement>('button.row')].find((b) =>
		b.textContent?.includes(name),
	);
	if (!found) throw new Error(`no row ${name}`);
	return found;
}

beforeAll(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
			commitStyles: vi.fn(),
			onfinish: null,
			playState: 'finished',
		})) as unknown as Element['animate'];
	}
});

beforeEach(() => {
	token.set('test-token');
	host = document.createElement('div');
	document.body.appendChild(host);
	vi.stubGlobal(
		'fetch',
		vi.fn(async (input: RequestInfo | URL) => {
			const path = new URL(String(input), 'http://x').searchParams.get('path') ?? '';
			const node = TREE[path];
			if (!node) return { ok: false, status: 400, json: async () => ({ detail: 'Bad path' }) };
			return { ok: true, json: async () => ({ path, parent: node.parent, dirs: node.dirs, files: [] }) };
		}),
	);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	token.set(null);
});

function open(onSelect = vi.fn()) {
	component = mount(PathPicker, {
		target: host,
		props: {
			title: 'Set project root',
			startPath: ROOT,
			rootPath: ROOT,
			canSelect: (path: string) => isProjectRootChoice(ROOT, path),
			onSelect,
			onClose: vi.fn(),
		},
	});
	return onSelect;
}

describe('PathPicker confined to the projects folder', () => {
	it('cannot choose the projects folder itself or leave it', async () => {
		open();
		await vi.waitFor(() => expect(document.body.textContent).toContain('alpha'));

		expect(chooseButton().disabled).toBe(true);
		expect(document.body.querySelector('button.up')).toBeNull();
	});

	it('cannot enter a symlinked folder', async () => {
		open();
		await vi.waitFor(() => expect(document.body.textContent).toContain('linked'));

		expect(rowButton('linked').disabled).toBe(true);
		expect(rowButton('alpha').disabled).toBe(false);
	});

	it('chooses a direct child folder', async () => {
		const onSelect = open();
		await vi.waitFor(() => expect(document.body.textContent).toContain('alpha'));

		rowButton('alpha').click();
		await vi.waitFor(() => expect(chooseButton().disabled).toBe(false));
		chooseButton().click();

		expect(onSelect).toHaveBeenCalledWith(`${ROOT}/alpha`);
	});

	it('cannot choose a folder nested deeper', async () => {
		const onSelect = open();
		await vi.waitFor(() => expect(document.body.textContent).toContain('alpha'));

		rowButton('alpha').click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('source'));
		rowButton('source').click();
		await vi.waitFor(() => expect(document.body.textContent).toContain(`${ROOT}/alpha/source`));

		expect(chooseButton().disabled).toBe(true);
		chooseButton().click();
		expect(onSelect).not.toHaveBeenCalled();
	});
});
