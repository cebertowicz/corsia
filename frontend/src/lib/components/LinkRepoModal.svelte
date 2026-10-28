<script lang="ts">
	import { Modal, Button, Field } from '$lib/components/ui';

	let { open, path, onPathChange, onSubmit, onCancel }: {
		open: boolean;
		path: string;
		onPathChange: (next: string) => void;
		onSubmit: () => void;
		onCancel: () => void;
	} = $props();
</script>

{#if open}
	<Modal title="Link a repository" size="sm" onClose={onCancel}>
		<Field variant="nested" layout="stacked" label="Folder path" labelFor="repo-folder-path">
			<input id="repo-folder-path" value={path} oninput={(event) => onPathChange(event.currentTarget.value)} placeholder="~/repos/corsia" spellcheck="false" />
		</Field>
		<p class="sheet-hint">Agents read these files where they are.</p>
		{#snippet foot()}
			<Button variant="ghost" onclick={onCancel}>Cancel</Button>
			<Button variant="primary" onclick={onSubmit}>Link repository</Button>
		{/snippet}
	</Modal>
{/if}
