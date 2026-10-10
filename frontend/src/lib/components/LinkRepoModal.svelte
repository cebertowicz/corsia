<script lang="ts">
	import { Modal, Button } from '$lib/components/ui';

	let { open, path, onPathChange, onSubmit, onCancel }: {
		open: boolean;
		path: string;
		onPathChange: (next: string) => void;
		onSubmit: () => void;
		onCancel: () => void;
	} = $props();
</script>

{#if open}
	<Modal title="Link a repo directory" size="sm" onClose={onCancel}>
		<label class="f"><span>Directory path</span><input value={path} oninput={(event) => onPathChange(event.currentTarget.value)} placeholder="~/repos/corsia" spellcheck="false" /></label>
		<p class="sheet-hint">Links into the project's source/ — the agent reads files in place; the sandbox allowlist picks it up automatically.</p>
		{#snippet foot()}
			<Button variant="ghost" onclick={onCancel}>Cancel</Button>
			<Button variant="primary" onclick={onSubmit}>Link directory</Button>
		{/snippet}
	</Modal>
{/if}
