import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { requestCorsiaMessage, requestSettingsSection, requestConnectedAppKey } = vi.hoisted(() => ({ requestCorsiaMessage: vi.fn(), requestSettingsSection: vi.fn(), requestConnectedAppKey: vi.fn() }));
vi.mock('$lib/corsiaRequest', () => ({ requestCorsiaMessage }));
vi.mock('$lib/stores/settingsNavigation', () => ({ requestSettingsSection, requestConnectedAppKey }));

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
	requestSettingsSection.mockReset();
	token.set(null);
});

function render(): { onClose: ReturnType<typeof vi.fn>; onConnected: ReturnType<typeof vi.fn>; onSignIn: ReturnType<typeof vi.fn> } {
	const onClose = vi.fn();
	const onConnected = vi.fn();
	const onSignIn = vi.fn();
	component = mount(ConnectAppFlow, { target: host, props: { onClose, onConnected, onSignIn } });
	return { onClose, onConnected, onSignIn };
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

async function connectResult(state: string, extra: Record<string, unknown> = {}): Promise<{ created: Record<string, unknown>; onClose: ReturnType<typeof vi.fn>; onSignIn: ReturnType<typeof vi.fn> }> {
	const created = { id: 'files', name: 'Files', state, revision: 1, reach: { mode: 'all', project_ids: [], outside_projects: true }, ...extra };
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => created })));
	const { onClose, onSignIn } = render();
	press('Continue');
	type('Name', 'Files');
	type('What it runs', 'npx -y @example/app');
	press('Continue');
	press('Connect');
	const stateLine = state === 'working'
		? `Connected${typeof extra.tool_count === 'number' && extra.tool_count ? ` · ${extra.tool_count} actions` : ''}`
		: state === 'needs_token' ? 'Needs a key' : state === 'needs_sign_in' ? 'Needs you to sign in' : state === 'connecting' ? 'Connecting…' : 'Disconnected';
	await vi.waitFor(() => expect(host.querySelector('h2')?.textContent).toBe(`Files: ${stateLine}`));
	return { created, onClose, onSignIn };
}

describe('ConnectAppFlow', () => {
	it('moves from What to the Mac setup step and confines the safety notice there', () => {
		render();
		expect(host.textContent).toContain('What');
		expect(host.textContent).toContain('Opens a chat with CORSIA to sign in.');
		expect(host.textContent).not.toContain('It can do anything you can do on this computer');
		press('Continue');
		expect(host.textContent).toContain('Set it up');
		expect(host.textContent).not.toContain('They may call it an MCP server.');
		expect(host.textContent).toContain("It can do anything you can do on this computer, except open CORSIA's own files and saved keys. Connect only apps you trust.");
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
		expect(host.textContent).not.toContain('It can do anything you can do on this computer');
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

	it('shows the fixed failure class and lets the person correct What it runs', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ id: 'files', name: 'Files', state: 'failed', failure_class: 'timed_out', revision: 1, reach: { mode: 'all', project_ids: [], outside_projects: true } }) })));
		render();
		press('Continue');
		type('Name', 'Files');
		type('What it runs', 'npx -y @example/app');
		press('Continue');
		press('Connect');
		await vi.waitFor(() => expect(host.textContent).toContain("Couldn't connect: no response"));
		expect(host.textContent).toContain("The app didn't respond within 10 seconds. Go back and compare What it runs with the app's setup instructions, then try again. It may also need something that isn't installed on your computer; its setup instructions say what.");
		expect(host.textContent).toContain('Try again');
		press('Back');
		await vi.waitFor(() => expect(host.querySelector('input[aria-label="What it runs"]')).toBe(document.activeElement));
	});

	it('shows working state as Connected and finishes with Done', async () => {
		const { onClose } = await connectResult('working', { tool_count: 46 });
		expect(host.querySelector('h2')?.textContent).toBe('Files: Connected · 46 actions');
		press('Done');
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('explains the key requirement and takes the person to Keys', async () => {
		const { onClose } = await connectResult('needs_token');
		expect(host.textContent).toContain('Files needs a key before agents can use it: a code Files gives you so other apps can use your account (Files may call it a token or an API key). Create one in your Files settings.');
		expect(host.textContent).toContain('Done');
		press('Set key');
		expect(requestSettingsSection).toHaveBeenCalledWith('keys');
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('opens the exact key card when the app declares its key', async () => {
		const refs = [{ name: 'FILES_TOKEN', kind: 'env', entry: 'files-token', set: false }];
		const { onClose } = await connectResult('needs_token', { key_refs: refs });
		press('Set key');
		expect(requestConnectedAppKey).toHaveBeenCalledWith('Files', 'files-token', refs);
		expect(requestSettingsSection).not.toHaveBeenCalled();
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('explains sign-in and opens the connected app after closing', async () => {
		const { created, onClose, onSignIn } = await connectResult('needs_sign_in');
		expect(host.textContent).toContain("Signing in lets the agents that can use an app act as you in it. You sign in on the app's own page in your browser, so CORSIA never sees your password.");
		expect(host.textContent).toContain('Done');
		press('Sign in');
		expect(onClose).toHaveBeenCalledOnce();
		expect(onSignIn).toHaveBeenCalledWith(created);
	});

	it('names connecting state without claiming the app is Connected', async () => {
		await connectResult('connecting');
		expect(host.querySelector('h2')?.textContent).toBe('Files: Connecting…');
		expect(host.querySelector('h2')?.textContent).not.toContain('Connected');
		expect(host.textContent).toContain('Done');
	});

	it('names disconnected state without claiming the app is Connected', async () => {
		await connectResult('disconnected');
		expect(host.querySelector('h2')?.textContent).toBe('Files: Disconnected');
		expect(host.querySelector('h2')?.textContent).not.toContain('Connected');
		expect(host.textContent).toContain('Done');
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
