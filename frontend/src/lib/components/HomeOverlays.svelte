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
	import { startUpdateScreen, updateScreenVisible } from '$lib/update/screenStore';
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

	onMount(() => startUpdateScreen(onUpdateFinished));
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
		title="Delete conversation?"
		description="This will permanently remove all messages. This cannot be undone."
		confirmLabel="Delete"
		destructive={true}
		onConfirm={onConfirmDeleteThread}
		onCancel={onCancelDeleteThread}
	/>
</div>
<UpdateScreen />
