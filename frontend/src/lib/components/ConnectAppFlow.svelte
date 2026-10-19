<script lang="ts">
	import { loadProjects, projects } from '$lib/stores/projects';
	import { Button, Modal } from '$lib/components/ui';
	import { createConnectedApp, type ConnectedApp } from '$lib/api/connectedApps';
	import { requestCorsiaMessage } from '$lib/corsiaRequest';

	let { onClose, onConnected }: { onClose: () => void; onConnected: (app: ConnectedApp) => void } = $props();
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
	const recoveryHint = $derived(error
		? kind === 'local' ? 'Check the command, then try again.' : 'Check the address, then try again.'
		: '');

	function selectKind(next: AppKind): void {
		kind = next;
		error = '';
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
			result = await createConnectedApp({
				name: name.trim(),
				config: kind === 'local' ? { command: command.trim() } : { url: address.trim() },
				reach,
			});
			onConnected(result);
			step = 'result';
		} catch (cause) {
			error = cause instanceof Error ? cause.message : "Couldn't connect. Try again.";
		} finally {
			submitting = false;
		}
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
					<label class="field"><span>What it runs</span><small>Paste what the app's setup instructions say to run</small><input aria-label="What it runs" value={command} oninput={(event) => command = event.currentTarget.value} placeholder="npx -y @example/app" /></label>
					<label class="field"><span>Name</span><small>What you'll see in CORSIA</small><input aria-label="Name" value={name} oninput={(event) => name = event.currentTarget.value} /></label>
					<p class="notice">It can do anything you can do on this computer, except open CORSIA's own files and saved keys. Connect only apps you trust.</p>
				{:else}
					<label class="field"><span>Web address</span><small>Paste the address from the app's setup instructions</small><input aria-label="Web address" value={address} oninput={(event) => suggestName(event.currentTarget.value)} placeholder="https://mcp.example.com" /></label>
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
			<section aria-live="polite"><h2>{result.name}: {result.state === 'working' ? `Connected${result.tool_count ? ` · ${result.tool_count} actions` : ''}` : result.state === 'needs_sign_in' ? 'Needs you to sign in' : result.state === 'needs_token' ? 'Needs a key' : 'Connected'}</h2></section>
		{/if}
		{#if error}<p class="error" role="alert">{error}</p><p class="recovery">{recoveryHint}</p>{/if}
	</div>
	{#snippet foot()}
		<div class="actions">
			{#if step === 'what'}
				<Button onclick={onClose} disabled={submitting}>Cancel</Button><Button variant="primary" onclick={continueFromWhat} loading={submitting}>Continue</Button>
			{:else if step === 'setup'}
				<Button onclick={() => { step = 'what'; error = ''; }}>Back</Button><Button variant="primary" onclick={continueFromSetup}>Continue</Button>
			{:else if step === 'where'}
				<Button onclick={() => { step = 'setup'; error = ''; }}>Back</Button><Button variant="primary" onclick={connect} disabled={!hasSelection} loading={submitting}>Connect</Button>
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
