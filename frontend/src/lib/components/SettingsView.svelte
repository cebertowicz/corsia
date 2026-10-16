<script lang="ts">
	import { tick } from 'svelte';
	import { loadProviders } from '$lib/stores/providers';
	import ProviderManager from './ProviderManager.svelte';
	import ProvidersPolicyBlock from './ProvidersPolicyBlock.svelte';
	import SettingsSystem from './SettingsSystem.svelte';
	import SystemBulletinsSetting from './SystemBulletinsSetting.svelte';
	import SettingsDebug from './SettingsDebug.svelte';
	import SettingsKeys from './SettingsKeys.svelte';
	import SettingsSkills from './SettingsSkills.svelte';
	import SettingsContext from './SettingsContext.svelte';
	import SettingsProjects from './SettingsProjects.svelte';
	import SettingsRow from './SettingsRow.svelte';
	import { loadServerVersion } from '$lib/version';
	import IconButton from '$lib/components/ui/IconButton.svelte';
	import { toastAnchorSurface } from '$lib/stores/toastAnchor';
	import { isMobile } from '$lib/stores/viewport';
	import { projects } from '$lib/stores/projects';
	import {
		returnToProjectPage,
		settingsActiveSection,
		settingsAnchorRequest,
		settingsDrill,
		settingsFromProject,
		settingsOpenRequest,
		settingsProject,
		type SettingsDrill,
		type SettingsSection,
	} from '$lib/stores/settingsNavigation';

	let { onClose, onBrowseContextFiles, onBrowseSkills, systemStatus = {} as any }: {
		onClose?: () => void;
		onBrowseContextFiles?: () => void;
		onBrowseSkills?: () => void;
		systemStatus?: { memory?: any; cpu?: any };
	} = $props();

	$effect(() => {
		if ($settingsActiveSection === 'providers') loadProviders();
		if ($settingsActiveSection === 'debug') loadServerVersion();
	});

	// A chat link such as /settings/system#restart lands on its section, then on the named element.
	$effect(() => {
		const anchor = $settingsAnchorRequest;
		if (!anchor) return;
		void $settingsActiveSection;
		void tick().then(() => {
			document.querySelector(`[data-settings-anchor="${anchor}"]`)?.scrollIntoView({ block: 'start' });
			settingsAnchorRequest.set(null);
		});
	});

	// Three groups by kind (S-R2); space alone separates them.
	const groups: { id: SettingsSection; label: string }[][] = [
		[
			{ id: 'providers', label: 'Providers' },
			{ id: 'skills', label: 'Skills' },
			{ id: 'context', label: 'Context' },
		],
		[{ id: 'projects', label: 'Projects' }],
		[
			{ id: 'keys', label: 'Keys' },
			{ id: 'system', label: 'System' },
			{ id: 'debug', label: 'Debug' },
		],
	];
	const labelOf = (id: SettingsSection) => groups.flat().find((s) => s.id === id)?.label ?? 'Settings';

	// Phone: the section list is the whole screen; a tapped row opens its section full-screen.
	let mobileOpen = $state(false);
	// A deep link (a gear, a chat link) opens its section, not the list.
	let seenRequest = $settingsOpenRequest;
	$effect(() => {
		const request = $settingsOpenRequest;
		if (request !== seenRequest) {
			seenRequest = request;
			mobileOpen = true;
		}
	});

	function select(id: SettingsSection) {
		settingsFromProject.set(null);
		settingsProject.set(null);
		settingsActiveSection.set(id);
		mobileOpen = true;
	}

	const projectName = $derived($projects.find((p) => p.id === $settingsProject)?.name ?? 'Project');

	// One back, in the app header: it names the page on a phone and goes up one level.
	$effect(() => {
		const phone = $isMobile;
		const section = $settingsActiveSection;
		const from = $settingsFromProject;
		let drill: SettingsDrill | null = null;
		if (from) {
			drill = { title: phone ? labelOf(section) : 'Settings', backLabel: from.name, onBack: returnToProjectPage };
		} else if (section === 'projects' && $settingsProject) {
			drill = { title: phone ? projectName : 'Settings', backLabel: 'Projects', onBack: () => settingsProject.set(null) };
		} else if (phone && mobileOpen) {
			drill = { title: labelOf(section), backLabel: 'Settings', onBack: () => { mobileOpen = false; } };
		}
		settingsDrill.set(drill);
	});
	$effect(() => () => settingsDrill.set(null));
</script>

<div class="settings-view">
	<nav class="desktop-nav" aria-label="Settings">
		<div class="nav-header">
			<span class="nav-title">Settings</span>
			{#if onClose}
				<IconButton size="sm" ariaLabel="Close settings" title="Close settings" onclick={onClose}>
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
				</IconButton>
			{/if}
		</div>
		{#each groups as group}
			<div class="nav-group">
				{#each group as s}
					<button
						class="nav-item"
						data-settings-section={s.id}
						class:active={$settingsActiveSection === s.id}
						aria-current={$settingsActiveSection === s.id ? 'page' : undefined}
						onclick={() => select(s.id)}
					>{s.label}</button>
				{/each}
			</div>
		{/each}
	</nav>

	<div class="settings-content" class:mobile-section={mobileOpen} use:toastAnchorSurface>
		<div class="section-host">
			{#if $settingsActiveSection === 'providers'}
				<ProviderManager />
				<div class="policy"><ProvidersPolicyBlock /></div>
			{:else if $settingsActiveSection === 'skills'}
				<h2 class="page-title">Skills</h2>
				<SettingsSkills {onBrowseSkills} />
			{:else if $settingsActiveSection === 'context'}
				<h2 class="page-title">Context</h2>
				<SettingsContext {onBrowseContextFiles} />
			{:else if $settingsActiveSection === 'projects'}
				<SettingsProjects />
			{:else if $settingsActiveSection === 'system'}
				<SettingsSystem {systemStatus} onSwitchSection={(s) => settingsActiveSection.set(s as SettingsSection)} />
				<SystemBulletinsSetting />
			{:else if $settingsActiveSection === 'keys'}
				<SettingsKeys />
			{:else if $settingsActiveSection === 'debug'}
				<SettingsDebug />
			{/if}
		</div>
	</div>

	<div class="mobile-root" class:hidden={mobileOpen}>
		{#each groups as group}
			<div class="mobile-group">
				{#each group as s}
					<div data-settings-section={s.id}>
						<SettingsRow name={s.label} chevron onOpen={() => select(s.id)} />
					</div>
				{/each}
			</div>
		{/each}
	</div>
</div>

<style>
	/* The shell header overlays the top of main; start below it so its buttons do not take this view's clicks. */
	.settings-view {
		display: flex; flex: 1; min-height: 0;
		background: var(--bg-primary);
		overflow: hidden; position: relative;
		padding-top: var(--header-bottom);
	}
	.desktop-nav {
		width: 200px; flex-shrink: 0;
		border-right: 1px solid var(--border-subtle);
		display: flex; flex-direction: column;
		padding: 0 var(--space-2) var(--space-4);
		overflow-y: auto;
	}
	.nav-header {
		display: flex; align-items: center; justify-content: space-between;
		height: var(--header-height);
		padding: 0 var(--space-1) 0 var(--space-2);
	}
	.nav-title { font-size: var(--text-body); font-weight: var(--fw-semibold); color: var(--text-primary); }
	.nav-group { display: flex; flex-direction: column; }
	.nav-group + .nav-group { margin-top: var(--space-4); }
	/* §0.1 list row: flat, hover wash, the selected row in the accent wash. */
	.nav-item {
		display: flex; align-items: center;
		min-height: var(--list-row-min-height); width: 100%;
		padding: 0 var(--space-2);
		background: none; border: none; border-radius: var(--radius-sm);
		cursor: pointer; font: inherit; font-size: var(--text-subhead); color: var(--text-primary);
		text-align: left;
		transition: background var(--dur-tight);
	}
	.nav-item:hover { background: var(--bg-hover); }
	.nav-item.active { background: var(--accent-wash); }
	.nav-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

	.settings-content { flex: 1; min-width: 0; overflow-y: auto; padding: var(--space-6) var(--space-8) var(--space-8); }
	.section-host { max-width: 720px; }
	.section-host :global(.page-title) {
		margin: 0 0 var(--space-5);
		padding: 0 var(--space-2);
		font-size: var(--text-title2); font-weight: var(--fw-semibold); color: var(--text-primary);
	}
	.policy { margin-top: var(--space-8); padding: 0 var(--space-2); }
	.mobile-root { display: none; }

	@media (max-width: 768px) {
		.settings-view { flex-direction: column; }
		.desktop-nav { display: none; }
		.settings-content { display: none; padding: var(--space-3) var(--space-3) var(--space-6); }
		.settings-content.mobile-section { display: block; }
		/* The app header names the page on a phone; the page title is not repeated under it. */
		.section-host :global(.page-title) { display: none; }
		.mobile-root { display: flex; flex-direction: column; flex: 1; overflow-y: auto; padding: var(--space-3); }
		.mobile-root.hidden { display: none; }
		.mobile-group { display: flex; flex-direction: column; }
		.mobile-group + .mobile-group { margin-top: var(--space-4); }
	}
</style>
