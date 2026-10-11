<script lang="ts">
	import InputBarControls from './InputBarControls.svelte';
	import QueuedInputStack from './QueuedInputStack.svelte';
	import type { Attachment, SheetView } from './inputBarHelpers';
	import { toastClearanceSurface } from '$lib/stores/toastClearance';

	let {
		inputEl = $bindable<HTMLDivElement | undefined>(),
		threadId,
		pendingAttachments,
		isMultiLine,
		sheetView,
		toggleSheet,
		isMobile,
		hasImageAttachments,
		inputText,
		hasMessageToSend,
		canSendToModel,
		queueUntilSwitch,
		onAttachFile,
		onAttachCamera,
		onAttachPhotos,
		onSendClick,
		onQueueAfterTurn,
		removeAttachment,
		handleKeydown,
		handleInput,
		handleFocus,
		handlePaste,
		onCompositionStart,
		onCompositionEnd,
	}: {
		inputEl?: HTMLDivElement;
		threadId: string | null;
		pendingAttachments: Attachment[];
		isMultiLine: boolean;
		sheetView: SheetView;
		toggleSheet: (view: Exclude<SheetView, null>) => void;
		isMobile: boolean;
		hasImageAttachments: boolean;
		inputText: string;
		hasMessageToSend: boolean;
		canSendToModel: boolean;
		queueUntilSwitch: boolean;
		onAttachFile: () => void;
		onAttachCamera: () => void;
		onAttachPhotos: () => void;
		onSendClick: () => void;
		onQueueAfterTurn: () => void;
		removeAttachment: (fileId: string) => void;
		handleKeydown: (event: KeyboardEvent) => void;
		handleInput: () => void;
		handleFocus: () => void;
		handlePaste: (event: ClipboardEvent) => void;
		onCompositionStart: () => void;
		onCompositionEnd: () => void;
	} = $props();

	let failedPreviewIds = $state<string[]>([]);

	function previewFailed(attachment: Attachment): boolean {
		return failedPreviewIds.includes(attachment.file_id);
	}

	function markPreviewFailed(attachment: Attachment): void {
		if (!previewFailed(attachment)) {
			failedPreviewIds = [...failedPreviewIds, attachment.file_id];
		}
	}
</script>

<div use:toastClearanceSurface>
	<QueuedInputStack {threadId} />

	<div class="input-chip" class:multi-line={isMultiLine}>
		{#if pendingAttachments.length > 0}
			<div class="attachment-previews">
				{#each pendingAttachments as att}
					<div class="attachment-chip">
						{#if att.is_image && att.preview_url && !previewFailed(att)}
							<img
								src={att.preview_url}
								alt={att.filename}
								class="attachment-thumb"
								onerror={() => markPreviewFailed(att)}
							/>
						{:else if att.is_image}
							<div class="attachment-icon" role="img" aria-label="Image attachment">
								<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
							</div>
						{:else}
							<div class="attachment-icon">
								<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
							</div>
						{/if}
						<span class="attachment-name">{att.filename}</span>
						<button
							class="attachment-remove"
							aria-label="Remove attachment"
							onclick={() => removeAttachment(att.file_id)}
						>
							<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6L6 18M6 6l12 12"/></svg>
						</button>
					</div>
				{/each}
			</div>
		{/if}

		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="composer"
			bind:this={inputEl}
			contenteditable="plaintext-only"
			role="textbox"
			aria-multiline="true"
			aria-label="Message CORSIA"
			tabindex="0"
			data-placeholder="Message CORSIA..."
			enterkeyhint="enter"
			inputmode="text"
			autocapitalize="sentences"
			{...{ autocorrect: 'on' }}
			spellcheck="true"
			onkeydown={handleKeydown}
			onfocus={handleFocus}
			oninput={handleInput}
			onpaste={handlePaste}
			oncompositionstart={onCompositionStart}
			oncompositionend={onCompositionEnd}
		></div>

		<InputBarControls
			surface="row"
			{threadId}
			{sheetView}
			{toggleSheet}
			{isMobile}
			{hasImageAttachments}
			{inputText}
			pendingAttachmentCount={pendingAttachments.length}
			{hasMessageToSend}
			{canSendToModel}
			{queueUntilSwitch}
			{onAttachFile}
			{onAttachCamera}
			{onAttachPhotos}
			{onSendClick}
			{onQueueAfterTurn}
		/>
	</div>
</div>
