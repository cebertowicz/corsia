<script lang="ts">
	import { tick } from 'svelte';
	import { loadProjects, projects } from '$lib/stores/projects';
	import { Button, Modal } from '$lib/components/ui';
	import { ConnectAppError, connectedAppStateLine, createConnectedApp, needsTokenBody, type ConnectedApp } from '$lib/api/connectedApps';
	import { SIGN_IN_LEDE } from '$lib/appSignIn';
	import { requestCorsiaMessage } from '$lib/corsiaRequest';
	import { requestConnectedAppKey, requestSettingsSection } from '$lib/stores/settingsNavigation';

	let { onClose, onConnected, onSignIn }: { onClose: () => void; onConnected: (app: ConnectedApp) => void; onSignIn: (app: ConnectedApp) => void } = $props();
	type AppKind = 'local' | 'web' | 'google';
	type Step = 'what' | 'setup' | 'where' | 'result';
	let step = $state<Step>('what');
	let kind = $state<AppKind>('local');
	let name = $state('');
	let command = $state('');
	let address = $state('');
	let mode = $state<'all' | 'selected'>('all');
	let selected = $state(new Set<string>());
	let outsideProjects = $state(true);
	let submitting = $state(false);
	let error = $state('');
	let recovery = $state('');
	let setupInput = $state<HTMLInputElement | undefined>(undefined);
	let result = $state<ConnectedApp | null>(null);

	void loadProjects();

	const hasSelection = $derived(mode === 'all' || selected.size > 0 || outsideProjects);
	const allNames = $derived([...$projects.map((project) => project.name), 'Outside projects']);
	const selectedNames = $derived([
		...$projects.filter((project) => selected.has(project.id)).map((project) => project.name),
		...(outsideProjects ? ['Outside projects'] : []),
	]);
	const reach = $derived(mode === 'all'
		? { mode: 'all' as const, project_ids: [], outside_projects: true }
		: { mode: 'selected' as const, project_ids: [...selected], outside_projects: outsideProjects });
	const reachLine = $derived(mode === 'all'
		? `Agents in ${allNames.join(', ')} and any new project will be able to use it.`
		: `Only agents in ${selectedNames.join(', ')} will be able to use it.`);
	function connectFailure(failureClass: string): { line: string; recovery: string } {
		const app = name.trim() || 'This app';
		switch (failureClass) {
			case 'cannot_start': return { line: "Couldn't connect: app didn't start", recovery: "The app didn't start on this computer. Go back and compare What it runs with the app's setup instructions, then try again. If it still won't start, it may need something that isn't installed on your computer; its setup instructions say what." };
			case 'timed_out': return kind === 'local'
				? { line: "Couldn't connect: no response", recovery: "The app didn't respond within 10 seconds. Go back and compare What it runs with the app's setup instructions, then try again. It may also need something that isn't installed on your computer; its setup instructions say what." }
				: { line: "Couldn't connect: no response", recovery: `${app} didn't respond within 10 seconds. Try again; if it keeps happening, ${app} may be down.` };
			case 'protocol_error': return { line: "Couldn't connect: not compatible", recovery: `${app} answered, but not in a way CORSIA understands. It may not be made for AI agents, or it may need an update. Check with its maker for an update.` };
			case 'name_collision': return { line: "Couldn't connect: clashes with another app", recovery: `CORSIA can't tell this app apart from another connected app. Go back and choose a different name, then try again.` };
			default: return { line: "Couldn't connect. Try again.", recovery: '' };
		}
	}

	function selectKind(next: AppKind): void {
		kind = next;
		error = '';
		recovery = '';
	}

	async function backToSetup(): Promise<void> {
		step = 'setup';
		error = '';
		recovery = '';
		await tick();
		setupInput?.focus();
	}

	function continueFromWhat(): void {
		error = '';
		if (kind === 'google') {
			connectGoogle();
			return;
		}
		step = 'setup';
	}

	function continueFromSetup(): void {
		error = '';
		if (!name.trim() || !(kind === 'local' ? command.trim() : address.trim())) {
			error = kind === 'local' ? 'Enter a name and what it runs.' : 'Enter a name and web address.';
			return;
		}
		step = 'where';
	}

	function useIndividualProjects(): void {
		mode = 'selected';
	}

	function useAllProjects(): void {
		mode = 'all';
	}

	function toggleProject(id: string): void {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function suggestName(value: string): void {
		address = value;
		if (name.trim()) return;
		try {
			const host = new URL(value).hostname.replace(/^www\./, '');
			const firstPart = host.split('.')[0] ?? '';
			if (firstPart) name = firstPart.charAt(0).toUpperCase() + firstPart.slice(1);
		} catch { /* An incomplete address remains unnamed until the person enters a name. */ }
	}

	function connectGoogle(): void {
		requestCorsiaMessage('Connect a Google account for all my projects');
		onClose();
	}

	async function connect(): Promise<void> {
		if (!hasSelection) return;
		submitting = true;
		error = '';
		try {
			const created = await createConnectedApp({
				name: name.trim(),
				config: kind === 'local' ? { command: command.trim() } : { url: address.trim() },
				reach,
			});
			if (created.state === 'failed') {
				const failure = connectFailure(created.failure_class ?? '');
				error = failure.line;
				recovery = failure.recovery;
				return;
			}
			result = created;
			onConnected(created);
			step = 'result';
		} catch (cause) {
			const failure = cause instanceof ConnectAppError ? connectFailure(cause.failureClass) : { line: "Couldn't connect. Try again.", recovery: '' };
			error = failure.line;
			recovery = failure.recovery;
		} finally {
			submitting = false;
		}
	}

	function openResultKey(): void {
		const refs = result?.key_refs ?? [];
		const ref = refs.find((candidate) => !candidate.set) ?? refs[0];
		if (result && ref) requestConnectedAppKey(result.name, ref.entry, refs);
		else requestSettingsSection('keys');
		onClose();
	}
</script>

<Modal title="Connect an app" size="sm" {onClose}>
	<div class="flow">
		{#if step === 'what'}
			<section aria-labelledby="connect-app-what">
				<h2 class="step-heading" id="connect-app-what">What</h2>
				<p class="hint">Follow the app's setup instructions. They may call it an MCP server.</p>
				<div class="choices">
					<label class="choice"><input type="radio" name="app-kind" checked={kind === 'local'} onchange={() => selectKind('local')} /><span><strong>An app that runs on this computer</strong><small>Its setup instructions give you something to run, often starting with npx or uvx.</small></span></label>
					<label class="choice"><input type="radio" name="app-kind" checked={kind === 'web'} onchange={() => selectKind('web')} /><span><strong>An app at a web address</strong><small>Its setup instructions give you an address starting with https://, on the internet or your network.</small></span></label>
					<label class="choice"><input type="radio" name="app-kind" checked={kind === 'google'} onchange={() => selectKind('google')} /><span><strong>Google (email, calendar and files)</strong><small>Opens a chat with CORSIA to sign in.</small></span></label>
				</div>
			</section>
		{:else if step === 'setup'}
			<section aria-labelledby="connect-app-setup">
				<h2 class="step-heading" id="connect-app-setup">Set it up</h2>
				{#if kind === 'local'}
					<label class="field"><span>What it runs</span><small>Paste what the app's setup instructions say to run</small><input bind:this={setupInput} aria-label="What it runs" value={command} oninput={(event) => command = event.currentTarget.value} placeholder="npx -y @example/app" /></label>
					<label class="field"><span>Name</span><small>What you'll see in CORSIA</small><input aria-label="Name" value={name} oninput={(event) => name = event.currentTarget.value} /></label>
					<p class="notice">It can do anything you can do on this computer, except open CORSIA's own files and saved keys. Connect only apps you trust.</p>
				{:else}
					<label class="field"><span>Web address</span><small>Paste the address from the app's setup instructions</small><input bind:this={setupInput} aria-label="Web address" value={address} oninput={(event) => suggestName(event.currentTarget.value)} placeholder="https://mcp.example.com" /></label>
					<label class="field"><span>Name</span><small>What you'll see in CORSIA</small><input aria-label="Name" value={name} oninput={(event) => name = event.currentTarget.value} /></label>
				{/if}
			</section>
		{:else if step === 'where'}
			<section aria-labelledby="connect-app-where">
				<h2 class="step-heading" id="connect-app-where">Where it works</h2>
				<div class="choices">
					<label class="choice"><input type="radio" name="reach" checked={mode === 'all'} onchange={useAllProjects} /><span><strong>All projects</strong><small>Includes new projects and Outside projects</small></span></label>
					<label class="choice"><input type="radio" name="reach" checked={mode === 'selected'} onchange={useIndividualProjects} /><span><strong>Select individually</strong></span></label>
					{#if mode === 'selected'}
						<div class="checklist">
							{#each $projects as project}
								<label><input type="checkbox" checked={selected.has(project.id)} onchange={() => toggleProject(project.id)} /> {project.name}</label>
							{/each}
							<label><input type="checkbox" checked={outsideProjects} onchange={() => outsideProjects = !outsideProjects} /> Outside projects</label>
						</div>
						{#if hasSelection}<p class="impact">{reachLine}</p>{:else}<p class="error" role="status">Select at least one project before connecting this app.</p>{/if}
					{/if}
				</div>
			</section>
		{:else if result}
			<section aria-live="polite">
				<h2>{result.name}: {connectedAppStateLine(result)}</h2>
				{#if result.state === 'needs_token'}<p class="recovery">{needsTokenBody(result.name)}</p>{/if}
				{#if result.state === 'needs_sign_in'}<p class="recovery">{SIGN_IN_LEDE}</p>{/if}
			</section>
		{/if}
		{#if error}<p class="error" role="alert">{error}</p>{#if recovery}<p class="recovery">{recovery}</p>{/if}{/if}
	</div>
	{#snippet foot()}
		<div class="actions">
			{#if step === 'what'}
				<Button onclick={onClose} disabled={submitting}>Cancel</Button><Button variant="primary" onclick={continueFromWhat} loading={submitting}>Continue</Button>
			{:else if step === 'setup'}
				<Button onclick={() => { step = 'what'; error = ''; }}>Back</Button><Button variant="primary" onclick={continueFromSetup}>Continue</Button>
			{:else if step === 'where'}
				<Button onclick={backToSetup}>Back</Button><Button variant="primary" onclick={connect} disabled={!hasSelection} loading={submitting}>{error ? 'Try again' : 'Connect'}</Button>
			{:else if result?.state === 'needs_token'}
				<Button onclick={onClose}>Done</Button><Button variant="primary" onclick={openResultKey}>Set key</Button>
			{:else if result?.state === 'needs_sign_in'}
				<Button onclick={onClose}>Done</Button><Button variant="primary" onclick={() => { onClose(); onSignIn(result); }}>Sign in</Button>
			{:else}
				<Button variant="primary" onclick={onClose}>Done</Button>
			{/if}
		</div>
	{/snippet}
</Modal>

<style>
	.flow, .choices { display: grid; gap: var(--space-3); }
	.flow section { display: grid; gap: var(--space-3); }
	.step-heading { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	section[aria-live] h2 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); }
	.hint, .choice small, .impact, .field small, .notice { color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.45; margin: 0; }
	.choice { display: flex; align-items: flex-start; gap: var(--space-3); cursor: pointer; color: var(--text-primary); }
	.choice input, .checklist input { accent-color: var(--accent); align-self: flex-start; flex: none; margin-top: var(--space-1); }
	.choice span, .field { display: grid; gap: var(--space-1); }
	.choice strong { font-size: var(--text-body); font-weight: var(--fw-regular); }
	.field > span { color: var(--text-primary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.field input { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-primary); color: var(--text-primary); font: inherit; padding: var(--space-2) var(--space-3); }
	.field input:focus { outline: none; border-color: var(--accent); }
	.checklist { display: grid; gap: var(--space-2); padding-left: var(--space-6); color: var(--text-secondary); }
	.checklist label { display: flex; align-items: flex-start; gap: var(--space-2); }
	.error { margin: 0; color: var(--red); font-size: var(--text-footnote); }
	.recovery { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.actions { display: flex; gap: var(--space-2); margin-left: auto; }
</style>
