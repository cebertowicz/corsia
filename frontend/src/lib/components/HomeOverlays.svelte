<script lang="ts">
	import { onMount } from 'svelte';
	import Lightbox from './Lightbox.svelte';
	import CommandPalette from './CommandPalette.svelte';
	import ToastContainer from './ToastContainer.svelte';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import VersionOverlay from './VersionOverlay.svelte';
	import CreateProjectModal from './CreateProjectModal.svelte';
	import UpdateProgressToast from '$lib/update/UpdateProgressToast.svelte';
	import UpdateScreen from './UpdateScreen.svelte';
	import { continueHandedOverScreen, startUpdateScreen, updateScreenVisible } from '$lib/update/screenStore';
	import { goto } from '$app/navigation';
	import { routeToRestore, takeHandoffFromLocation } from '$lib/update/shellHandoff';
	import type { ProjectPick } from '$lib/stores/projects';

	let {
		lightboxUrl = $bindable(''),
		lightboxOpen = $bindable(false),
		deleteConfirmId,
		onPickProject,
		onPickConversation,
		onNewChat,
		onGoWorkspace,
		onProjectCreated,
		onConfirmDeleteThread,
		onCancelDeleteThread,
		onUpdateFinished,
	}: {
		lightboxUrl: string;
		lightboxOpen: boolean;
		deleteConfirmId: string | null;
		onPickProject: (project: ProjectPick) => void;
		onPickConversation: (id: string) => void;
		onNewChat: () => void;
		onGoWorkspace: () => void;
		onProjectCreated: (project: ProjectPick) => void;
		onConfirmDeleteThread: () => void | Promise<void>;
		onCancelDeleteThread: () => void;
		onUpdateFinished?: () => void | Promise<void>;
	} = $props();

	onMount(() => {
		const stop = startUpdateScreen(onUpdateFinished);
		// Back from the shell's update page: the same screen continues until this page confirms the new version.
		const handedBack = takeHandoffFromLocation();
		if (handedBack) {
			continueHandedOverScreen(handedBack, false);
			const route = routeToRestore(handedBack.route, window.location.pathname, window.location.search);
			if (route) void goto(route, { replaceState: true, noScroll: true, keepFocus: true });
		}
		return stop;
	});
</script>

<div inert={$updateScreenVisible} aria-hidden={$updateScreenVisible}>
	<Lightbox bind:url={lightboxUrl} bind:open={lightboxOpen} />
	<ToastContainer />
	<VersionOverlay />
	<UpdateProgressToast />

	<CommandPalette
		onPickProject={onPickProject}
		onPickConversation={onPickConversation}
		onNewChat={onNewChat}
		onGoWorkspace={onGoWorkspace}
	/>

	<CreateProjectModal onCreated={onProjectCreated} />

	<ConfirmDialog
		open={!!deleteConfirmId}
		title="Delete chat?"
		description="This will permanently remove all messages. This cannot be undone."
		confirmLabel="Delete"
		destructive={true}
		onConfirm={onConfirmDeleteThread}
		onCancel={onCancelDeleteThread}
	/>
</div>
<UpdateScreen />
