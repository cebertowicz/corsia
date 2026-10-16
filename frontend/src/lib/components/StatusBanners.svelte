<script lang="ts">
	import { onDestroy } from 'svelte';
	import { connectionState, drainStatus, refreshDrainStatus, securityAlert, dismissSecurityAlert } from '$lib/stores/connection';
	import type { DrainStatus } from '$lib/stores/connection';
	import { token } from '$lib/stores/auth';
	import { reconnectNow } from '$lib/ws';
	import { updateOperation } from '$lib/update/store';
	import { holdsNewWork } from '$lib/update/model';
	import { toastTopSurface } from '$lib/stores/toastTop';

	function blockingWork(status: Exclude<DrainStatus, null>): string {
		const parts: string[] = [];
		const add = (count: number, singular: string, plural = `${singular}s`) => {
			if (count > 0) parts.push(`${count} ${count === 1 ? singular : plural}`);
		};
		add(status.turnAdmissions, 'turn admission');
		add(status.activeTurns, 'active turn');
		add(status.hostAccessSupervisors, 'host-access supervisor');
		add(status.ownedProcessGroups, 'owned process group');
		add(status.browserCommands, 'browser command');
		return parts.join(', ') || `${status.activeProcesses} blocking work item${status.activeProcesses === 1 ? '' : 's'}`;
	}

	function additionalBlockingWork(status: Exclude<DrainStatus, null>): string {
		const parts: string[] = [];
		const add = (count: number, singular: string, plural = `${singular}s`) => {
			if (count > 0) parts.push(`${count} ${count === 1 ? singular : plural}`);
		};
		add(
			Math.max(status.activeTurns, status.blockingTurns.length) - 1,
			'more active turn',
			'more active turns',
		);
		add(status.turnAdmissions, 'turn admission');
		add(status.hostAccessSupervisors, 'host-access supervisor');
		add(status.ownedProcessGroups, 'owned process group');
		add(status.browserCommands, 'browser command');
		return parts.join(', ');
	}

	$: updateOwnsDrain = holdsNewWork($updateOperation);

	let drainRefreshTimer: ReturnType<typeof setInterval> | null = null;
	async function refreshDrain(): Promise<void> {
		if (!$token) return;
		try {
			await refreshDrainStatus($token);
		} catch (error) {
			console.warn('[drain] status refresh failed', error);
		}
	}
	function stopDrainRefresh(): void {
		if (drainRefreshTimer !== null) clearInterval(drainRefreshTimer);
		drainRefreshTimer = null;
	}
	$: if ($drainStatus?.active && $token) {
		if (drainRefreshTimer === null) {
			drainRefreshTimer = setInterval(() => void refreshDrain(), 1000);
		}
	} else {
		stopDrainRefresh();
	}
	onDestroy(stopDrainRefresh);
</script>

<div class="status-banners">
{#if $securityAlert}
	<div class="security-banner" use:toastTopSurface>
		<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
		<span>
			Security audit ({$securityAlert.date}): {$securityAlert.vulns} vulnerabilit{$securityAlert.vulns === 1 ? 'y' : 'ies'}
			{#if $securityAlert.errors > 0}+ {$securityAlert.errors} error{$securityAlert.errors === 1 ? '' : 's'}{/if}
			found — review <code>{$securityAlert.report}</code>
		</span>
		<button class="dismiss-btn" onclick={() => $token && dismissSecurityAlert($token)}>Dismiss</button>
	</div>
{/if}

{#if $drainStatus?.active && !updateOwnsDrain}
	<div class="drain-banner" use:toastTopSurface>
		<svg class="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		<span>
			{#if $drainStatus.countdown != null}
				Restarting in {$drainStatus.countdown}s...
			{:else if $drainStatus.blockingTurns.length > 0}
				Restart pending — waiting on “{$drainStatus.blockingTurns[0].title}” ({$drainStatus.blockingTurns[0].threadId}){#if additionalBlockingWork($drainStatus)} and {additionalBlockingWork($drainStatus)}{/if}
			{:else if $drainStatus.activeProcesses > 0}
				Restart pending — waiting on {blockingWork($drainStatus)}
			{:else}
				Restart pending — draining...
			{/if}
		</span>
	</div>
{:else if $connectionState === 'disconnected' || $connectionState === 'error'}
	<div class="connection-banner" use:toastTopSurface>
		<svg class="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		<span>{$connectionState === 'error' ? 'Connection error' : 'Reconnecting...'}</span>
		<button class="reconnect-btn" onclick={reconnectNow}>Retry now</button>
	</div>
{/if}
</div>

<style>
	/* The header is laid over the top of the column, so a banner in flow would sit behind it. This strip sits
	   just below the header but keeps its height in the flow (the negative margin cancels the offset), so the
	   content still moves down by the banner height, as before, and no view's first line sits under a banner. */
	.status-banners {
		--status-banners-offset: var(--header-bottom);
		position: relative; z-index: 9;
		margin-top: var(--status-banners-offset); margin-bottom: calc(-1 * var(--status-banners-offset));
		flex-shrink: 0;
	}
	.security-banner {
		background: #dc2626; color: white;
		padding: 6px 16px; font-size: var(--text-subhead); font-weight: 500;
		display: flex; align-items: center; gap: 8px;
		justify-content: center; flex-shrink: 0;
	}
	.security-banner code {
		font-size: var(--text-footnote); background: rgba(255,255,255,0.15);
		padding: 1px 5px; border-radius: var(--radius-2xs);
	}
	.dismiss-btn {
		padding: 2px 10px; border-radius: var(--radius-2xs);
		background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.3);
		color: white; font-size: var(--text-footnote); font-weight: 600;
		cursor: pointer; font-family: inherit;
	}
	.dismiss-btn:hover { background: rgba(255,255,255,0.3); }
	.drain-banner {
		background: var(--accent-subtle); color: var(--violet-text);
		padding: 6px 16px; font-size: var(--text-subhead); font-weight: 500;
		display: flex; align-items: center; gap: 8px;
		justify-content: center; flex-shrink: 0;
	}
	.connection-banner {
		background: var(--yellow); color: #1a1a1e;
		padding: 6px 16px; font-size: var(--text-subhead); font-weight: 500;
		display: flex; align-items: center; gap: 8px;
		justify-content: center; flex-shrink: 0;
	}
	.reconnect-btn {
		padding: 2px 10px; border-radius: var(--radius-2xs);
		background: rgba(0,0,0,0.15); border: 1px solid rgba(0,0,0,0.2);
		color: #1a1a1e; font-size: var(--text-footnote); font-weight: 600;
		cursor: pointer; font-family: inherit;
	}
	.reconnect-btn:hover { background: rgba(0,0,0,0.25); }
</style>
