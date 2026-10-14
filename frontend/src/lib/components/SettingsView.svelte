<script lang="ts">
	import { tick } from 'svelte';
	import { loadProviders } from '$lib/stores/providers';
	import ProviderManager from './ProviderManager.svelte';
	import SettingsSystem from './SettingsSystem.svelte';
	import SystemBulletinsSetting from './SystemBulletinsSetting.svelte';
	import SettingsDebug from './SettingsDebug.svelte';
	import SettingsKeys from './SettingsKeys.svelte';
	import { loadServerVersion } from '$lib/version';
	import IconButton from '$lib/components/ui/IconButton.svelte';
	import { toastAnchorSurface } from '$lib/stores/toastAnchor';
	import { toastClearanceSurface } from '$lib/stores/toastClearance';
	import { toastStackHeight } from '$lib/stores/toastStack';
	import { settingsActiveSection, settingsAnchorRequest, type SettingsSection } from '$lib/stores/settingsNavigation';

	let { onClose, systemStatus = {} as any }: {
		onClose?: () => void;
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

	const sections: { id: SettingsSection; label: string; icon: string }[] = [
		{ id: 'providers', label: 'Providers', icon: 'M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z' },
		{ id: 'system',    label: 'System',    icon: 'M12 20h9M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z' },
		{ id: 'keys',      label: 'Keys',      icon: 'M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4' },
		{ id: 'debug',     label: 'Debug',     icon: 'M8 2v4M16 2v4M9 18h6M7 9h10v8a3 3 0 01-3 3h-4a3 3 0 01-3-3zM3 12h4M17 12h4M5 5l3 3M19 5l-3 3' },
	];
</script>

<div class="settings-view">
	<nav class="settings-nav desktop-nav">
		<div class="settings-nav-header">
			<span class="settings-nav-title">Settings</span>
			{#if onClose}
				<IconButton size="sm" ariaLabel="Close settings" title="Close settings" onclick={onClose}>
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M18 6L6 18M6 6l12 12"/>
					</svg>
				</IconButton>
			{/if}
		</div>
		{#each sections as s}
			<button
				class="nav-item press-tint"
				class:active={$settingsActiveSection === s.id}
				onclick={() => { settingsActiveSection.set(s.id); }}
			>
				<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
					<path d={s.icon}/>
				</svg>
				{s.label}
			</button>
		{/each}
	</nav>

	<div class="settings-content" use:toastAnchorSurface style:padding-bottom={$toastStackHeight > 0 ? `calc(var(--space-5) + ${$toastStackHeight}px + var(--space-6) + var(--space-3))` : undefined}>
		{#if $settingsActiveSection === 'providers'}
			<div class="providers-host">
				<ProviderManager />
			</div>
		{:else if $settingsActiveSection === 'system'}
			<SettingsSystem {systemStatus} onSwitchSection={(s) => { settingsActiveSection.set(s as SettingsSection); }} />
			<SystemBulletinsSetting />
		{:else if $settingsActiveSection === 'keys'}
			<SettingsKeys />
		{:else if $settingsActiveSection === 'debug'}
			<SettingsDebug />
		{/if}
	</div>

	<div class="mobile-tab-bar" use:toastClearanceSurface>
		{#each sections as s}
			<button
				class="mobile-tab press-tint"
				class:active={$settingsActiveSection === s.id}
				onclick={() => { settingsActiveSection.set(s.id); }}
			>
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
					<path d={s.icon}/>
				</svg>
				{s.label}
			</button>
		{/each}
	</div>
</div>

<style>
	/* The shell header overlays the top of main; start below it, as the project
	   settings view does, so its buttons do not take this view's clicks. */
	.settings-view { display: flex; flex: 1; min-height: 0; background: var(--bg-primary); overflow: hidden; position: relative; padding-top: calc(var(--header-height, 60px) + env(safe-area-inset-top, 0px)); }

	.desktop-nav { width: 200px; flex-shrink: 0; background: var(--glass-thick); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur); box-shadow: var(--glass-rim); border-right: 1px solid var(--border-subtle); display: flex; flex-direction: column; padding: 0; }
	.settings-nav-header { display: flex; align-items: center; justify-content: space-between; padding: 20px 16px 12px; border-bottom: 1px solid var(--border-subtle); }
	.settings-nav-title { font-size: var(--text-caption); font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em; }

	.nav-item { display: flex; align-items: center; gap: 9px; padding: 9px 16px; background: none; border: none; cursor: pointer; font-size: var(--text-callout); color: var(--text-secondary); text-align: left; width: 100%; border-radius: 0; transition: background var(--dur-tight), color var(--dur-tight); font-family: inherit; }
	.nav-item:hover { background: var(--bg-hover); color: var(--text-primary); }
	.nav-item.active { background: var(--accent-subtle); color: var(--accent); font-weight: 500; }
	.nav-item svg { flex-shrink: 0; opacity: 0.8; }
	.nav-item.active svg { opacity: 1; }

	.settings-content { flex: 1; overflow-y: auto; padding: 32px 40px; }

	.mobile-tab-bar { display: none; }
	.mobile-tab { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px; padding: 8px 4px 10px; background: none; border: none; color: var(--text-tertiary); font-family: inherit; font-size: var(--text-caption); font-weight: 500; cursor: pointer; -webkit-tap-highlight-color: transparent; transition: color var(--dur-tight); }
	.mobile-tab:active { border-radius: var(--radius-sm); }
	.mobile-tab:hover { color: var(--text-secondary); }
	.mobile-tab.active { color: var(--accent); }

	.providers-host {
		background: transparent;
		border: 0;
		padding: 8px 24px 8px;
		margin-bottom: 16px;
	}

	@media (max-width: 768px) {
		.settings-view { flex-direction: column; }
		.desktop-nav { display: none; }
		.settings-content { padding: 12px 12px 16px; }
		.providers-host { background: transparent; border: 0; padding: 0; margin: 0; }
		.mobile-tab-bar {
			display: flex; flex-shrink: 0;
			border-top: 1px solid var(--border-subtle);
			background: var(--bg-primary);
			padding-bottom: max(10px, env(safe-area-inset-bottom, 0px));
		}
	}
</style>
