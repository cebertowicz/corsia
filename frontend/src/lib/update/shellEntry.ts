/**
 * The desktop shell's update page (desktop/dist-shell/update.html), built into a classic script by
 * desktop/scripts/gen-shell-update.mjs. It draws the same update screen the app draws while the service is down,
 * continuing from the screen the local-service page handed over, and is the only page that may ask the shell for a
 * recovery start. With no valid handoff there is no update to show, so it goes to the ordinary startup page.
 *
 * An app the update reopened opens its window here with the screen it was handed while its launch start runs as it
 * always does. The shell draws that start's answer on every page it loads, through the hooks below: Ready returns to
 * the service's page like the shell's own ready reading; every other answer returns to the startup page, where native
 * ShellState and its normal hook render the answer's existing guidance.
 */
import '../../app.css';
import { mount } from 'svelte';
import UpdateScreen from '$lib/components/UpdateScreen.svelte';
import { markStartupShellPage } from '$lib/remoteShell';
import { continueHandedOverScreen, handOverScreen, startUpdateScreen } from './screenStore';
import { encodeHandoff, keepReturnRoute, readHandoff } from './shellHandoff';
import { launchReadyUrl, refreshStartupProgress } from './startupProgress';
import { updateUnreachable } from './store';

const shellHooks = window as unknown as { __corsiaShowStartup?: (startup: unknown) => void; __corsiaShowServiceFailure?: (reason: unknown) => void };
function showStartupShell(): void {
	window.location.replace('index.html');
}
shellHooks.__corsiaShowStartup = (startup) => {
	const answer = startup && typeof startup === 'object' ? (startup as { state?: unknown; url?: unknown }) : null;
	if (answer?.state === 'ready' && typeof answer.url === 'string') launchReadyUrl.set(answer.url);
	else showStartupShell();
};
shellHooks.__corsiaShowServiceFailure = () => showStartupShell();

async function begin(): Promise<void> {
	const handoff = readHandoff(window.location.hash);
	if (!handoff) {
		window.location.replace('index.html');
		return;
	}
	markStartupShellPage();
	document.documentElement.dataset.theme = handoff.theme;
	keepReturnRoute(handoff.route);
	// This page exists only while the service is down; the screen reads the PID-validated progress file on its usual cadence.
	updateUnreachable.set(true);
	startUpdateScreen(undefined, () => {
		void refreshStartupProgress();
		// A reload continues from the latest screen, on the screen's existing clock.
		const screen = handOverScreen(handoff.theme, handoff.route);
		if (screen) history.replaceState(null, '', `#${encodeHandoff(screen)}`);
	});
	// Read the record first: without it the stall clock would look silent and the screen would reach "longer" early.
	await refreshStartupProgress();
	continueHandedOverScreen(handoff, true);
	mount(UpdateScreen, { target: document.body });
}

if (document.readyState === 'complete') void begin();
else window.addEventListener('load', () => void begin(), { once: true });
