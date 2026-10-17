import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { requestCorsiaMessage } = vi.hoisted(() => ({ requestCorsiaMessage: vi.fn() }));
vi.mock('$lib/corsiaRequest', () => ({ requestCorsiaMessage }));

import ConnectAppFlow from './ConnectAppFlow.svelte';
import { token } from '$lib/stores/auth';
import { _setProjectsForTests } from '$lib/stores/projects';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const project = { id: 'garden', name: 'Garden plan', status: 'active', accent_color: null };

beforeEach(() => {
	token.set('test-token');
	_setProjectsForTests([project]);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	requestCorsiaMessage.mockReset();
	token.set(null);
});

function render(): { onClose: ReturnType<typeof vi.fn>; onConnected: ReturnType<typeof vi.fn> } {
	const onClose = vi.fn();
	const onConnected = vi.fn();
	component = mount(ConnectAppFlow, { target: host, props: { onClose, onConnected } });
	return { onClose, onConnected };
}

function press(label: string): void {
	const button = [...host.querySelectorAll('button')].find((candidate) => candidate.textContent?.trim() === label) as HTMLButtonElement;
	expect(button).toBeTruthy();
	button.click();
	flushSync();
}

function type(label: string, value: string): void {
	const field = host.querySelector(`input[aria-label="${label}"]`) as HTMLInputElement;
	field.value = value;
	field.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

describe('ConnectAppFlow', () => {
	it('moves from What to the Mac setup step and confines the safety notice there', () => {
		render();
		expect(host.textContent).toContain('What');
		expect(host.textContent).toContain('Opens a chat with CORSIA to sign in.');
		expect(host.textContent).not.toContain('It can do anything you can do on this Mac');
		press('Continue');
		expect(host.textContent).toContain('Set it up');
		expect(host.textContent).not.toContain('They may call it an MCP server.');
		expect(host.textContent).toContain("It can do anything you can do on this Mac, except open CORSIA's own files and saved keys. Connect only apps you trust.");
	});

	it('asks for the connection detail before the name for each app kind', () => {
		render();
		press('Continue');
		expect([...host.querySelectorAll('input')].map((input) => input.getAttribute('aria-label'))).toEqual(['What it runs', 'Name']);
		press('Back');
		const web = [...host.querySelectorAll('input[type="radio"]')].find((input) => input.parentElement?.textContent?.includes('web address')) as HTMLInputElement;
		web.click();
		press('Continue');
		expect([...host.querySelectorAll('input')].map((input) => input.getAttribute('aria-label'))).toEqual(['Web address', 'Name']);
	});

	it('keeps an empty setup form in place with a direct error', () => {
		render();
		press('Continue');
		press('Continue');
		expect(host.textContent).toContain('Enter a name and what it runs.');
		expect(host.textContent).toContain('Set it up');
	});

	it('shows Where it works after a complete setup without the setup hint or safety notice', () => {
		render();
		press('Continue');
		type('Name', 'Files');
		type('What it runs', 'npx -y @example/app');
		press('Continue');
		expect(host.textContent).toContain('Where it works');
		expect(host.textContent).toContain('All projects');
		expect(host.textContent).not.toContain('Agents in Garden plan, Outside projects and any new project will be able to use it.');
		(host.querySelectorAll('input[type="radio"]')[1] as HTMLInputElement).click();
		flushSync();
		expect(host.textContent).toContain('Only agents in Outside projects will be able to use it.');
		expect(host.textContent).not.toContain('They may call it an MCP server.');
		expect(host.textContent).not.toContain('It can do anything you can do on this Mac');
	});

	it('posts the local setup and reach only after the final Connect action', async () => {
		const created = { id: 'files', name: 'Files', state: 'working', revision: 1, tool_count: 46, reach: { mode: 'all', project_ids: [], outside_projects: true } };
		const fetch = vi.fn(async () => ({ ok: true, json: async () => created }));
		vi.stubGlobal('fetch', fetch);
		const { onConnected } = render();
		press('Continue');
		type('Name', 'Files');
		type('What it runs', 'npx -y @example/app');
		press('Continue');
		press('Connect');
		await vi.waitFor(() => expect(onConnected).toHaveBeenCalledWith(created));
		expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers'), expect.objectContaining({ method: 'POST' }));
		expect(host.textContent).toContain('Files: Connected · 46 actions');
	});

	it('shows a connection error without leaving the final step', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({ detail: 'The app did not answer' }) })));
		render();
		press('Continue');
		type('Name', 'Files');
		type('What it runs', 'npx -y @example/app');
		press('Continue');
		press('Connect');
		await vi.waitFor(() => expect(host.textContent).toContain('The app did not answer'));
		expect(host.textContent).toContain('Where it works');
		expect(host.textContent).toContain('Check the command, then try again.');
	});

	it('opens CORSIA with the Google request instead of starting OAuth', () => {
		const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ projects: [] }) }));
		vi.stubGlobal('fetch', fetch);
		const { onClose } = render();
		const google = [...host.querySelectorAll('input[type="radio"]')].find((input) => input.parentElement?.textContent?.includes('Google')) as HTMLInputElement;
		google.click();
		press('Continue');
		expect(requestCorsiaMessage).toHaveBeenCalledOnce();
		expect(requestCorsiaMessage).toHaveBeenCalledWith('Connect a Google account for all my projects');
		expect(fetch.mock.calls.some(([input]) => String(input).includes('/api/google/connect'))).toBe(false);
		expect(onClose).toHaveBeenCalledOnce();
	});
});
