<script lang="ts">
	import { loadProviders } from '$lib/stores/providers';
	import ProviderManager from './ProviderManager.svelte';
	import SettingsSystem from './SettingsSystem.svelte';
	import SystemBulletinsSetting from './SystemBulletinsSetting.svelte';
	import SettingsDebug from './SettingsDebug.svelte';
	import SettingsKeys from './SettingsKeys.svelte';
	import SettingsSkills from './SettingsSkills.svelte';
	import SettingsContext from './SettingsContext.svelte';
	import SettingsProjects from './SettingsProjects.svelte';
	import SettingsRow from './SettingsRow.svelte';
	import ConnectedAppsSettings from './ConnectedAppsSettings.svelte';
	import { loadServerVersion } from '$lib/version';
	import IconButton from '$lib/components/ui/IconButton.svelte';
	import { toastAnchorSurface } from '$lib/stores/toastAnchor';
	import { isMobile } from '$lib/stores/viewport';
	import { projects } from '$lib/stores/projects';
	import {
		closeSettingsProject,
		consumeSettingsOpenRequest,
		returnToProjectPage,
		returnToConnectedApps,
		selectSettingsSection,
		setSettingsDrill,
		settingsNavigation,
		type SettingsDrill,
		type SettingsSection,
	} from '$lib/stores/settingsNavigation';

	let { onClose, onBrowseContextFiles, onBrowseSkills, systemStatus = {} as any }: {
		onClose?: () => void;
		onBrowseContextFiles?: () => void;
		onBrowseSkills?: () => void;
		systemStatus?: { memory?: any; cpu?: any };
	} = $props();

	// Effects read these, not the whole store: the drill this view writes must not re-run them.
	const section = $derived($settingsNavigation.section);
	const project = $derived($settingsNavigation.project);
	const fromProject = $derived($settingsNavigation.fromProject);
	const connectedAppKey = $derived($settingsNavigation.connectedAppKey);
	const openRequest = $derived($settingsNavigation.openRequest);

	$effect(() => {
		if (section === 'providers') loadProviders();
		if (section === 'debug') loadServerVersion();
	});

	// Three groups by kind (S-R2); space alone separates them.
	const groups: { id: SettingsSection; label: string }[][] = [
		[
			{ id: 'providers', label: 'Providers' },
			{ id: 'connected-apps', label: 'Connected apps' },
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
	// Pure view state of this mounted view, so it stays local.
	let mobileOpen = $state(false);
	// A deep link (a gear, a chat link) opens its section, not the list, whether it opened
	// Settings or arrived while Settings was already showing.
	$effect(() => {
		void openRequest;
		if (consumeSettingsOpenRequest()) mobileOpen = true;
	});

	function select(id: SettingsSection) {
		selectSettingsSection(id);
		mobileOpen = true;
	}

	const projectName = $derived($projects.find((p) => p.id === project)?.name ?? 'Project');

	// One back, in the app header: it names the page on a phone and goes up one level.
	$effect(() => {
		const phone = $isMobile;
		const from = fromProject;
		const key = connectedAppKey;
		let drill: SettingsDrill | null = null;
		if (key) {
			drill = { title: phone ? 'Keys' : 'Settings', backLabel: 'Connected apps', onBack: returnToConnectedApps };
		} else if (from) {
			drill = { title: phone ? labelOf(section) : 'Settings', backLabel: from.name, onBack: returnToProjectPage };
		} else if (section === 'projects' && project) {
			drill = { title: projectName, backLabel: 'Projects', onBack: closeSettingsProject };
		} else if (phone && mobileOpen) {
			drill = { title: labelOf(section), backLabel: 'Settings', onBack: () => { mobileOpen = false; } };
		}
		setSettingsDrill(drill);
	});
	$effect(() => () => setSettingsDrill(null));
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
						class:active={section === s.id}
						aria-current={section === s.id ? 'page' : undefined}
						onclick={() => select(s.id)}
					>{s.label}</button>
				{/each}
			</div>
		{/each}
	</nav>

	<div class="settings-content" class:mobile-section={mobileOpen} use:toastAnchorSurface>
		<!-- A phone hides this content until a section is open, so the section mounts only then: an anchored element
			 consumes its request at mount, and a mount in hidden content has no box to scroll to. -->
		{#if !$isMobile || mobileOpen}
		<div class="section-host">
			{#if section === 'providers'}
				<ProviderManager />
			{:else if section === 'connected-apps'}
				<h2 class="page-title">Connected apps</h2>
				<ConnectedAppsSettings />
			{:else if section === 'skills'}
				<h2 class="page-title">Skills</h2>
				<SettingsSkills {onBrowseSkills} />
			{:else if section === 'context'}
				<h2 class="page-title">Context</h2>
				<SettingsContext {onBrowseContextFiles} />
			{:else if section === 'projects'}
				<SettingsProjects />
			{:else if section === 'system'}
				<h2 class="page-title">System</h2>
				<SettingsSystem {systemStatus} onSwitchSection={(s) => selectSettingsSection(s as SettingsSection)} />
				<SystemBulletinsSetting />
			{:else if section === 'keys'}
				<h2 class="page-title">Keys</h2>
				<SettingsKeys />
			{:else if section === 'debug'}
				<h2 class="page-title">Debug</h2>
				<SettingsDebug />
			{/if}
		</div>
		{/if}
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
	.mobile-root { display: none; }

	@media (max-width: 768px) {
		.settings-view { flex-direction: column; }
		.desktop-nav { display: none; }
		.settings-content { display: none; padding: var(--space-3) var(--space-3) var(--space-6); }
		.settings-content.mobile-section { display: block; }
		/* The app header names the page on a phone; the title is not drawn again under it but stays the page's heading for assistive tech. */
		.section-host :global(.page-title) {
			position: absolute; width: 1px; height: 1px; padding: 0; margin: 0;
			overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
		}
		.section-host :global(.page-head) { justify-content: flex-end; }
		/* The visually hidden title remains the first DOM child; do not let it create an extra group gap. */
		.section-host > :global(.settings-group:first-of-type) { margin-top: 0; }
		.mobile-root { display: flex; flex-direction: column; flex: 1; overflow-y: auto; padding: var(--space-3); }
		.mobile-root.hidden { display: none; }
		.mobile-group { display: flex; flex-direction: column; }
		.mobile-group + .mobile-group { margin-top: var(--space-4); }
	}
</style>
