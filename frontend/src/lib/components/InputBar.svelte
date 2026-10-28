<script lang="ts">
	import './InputBar.css';
	import { tick, onMount } from 'svelte';
	import { setContentEditableText } from '$lib/inputText';
	import { token } from '$lib/stores/auth';
	import { activeThreadId, isThreadTurnActive } from '$lib/stores/threads';
	import { composerSelectionFor } from '$lib/stores/composerSelection';
	import { turnStates } from '$lib/ws/turnState';
	import InputBarComposer from './InputBarComposer.svelte';
	import ChatCommentSelection from './ChatCommentSelection.svelte';
	import type { ChatComment } from '$lib/chatCommentAnchors';
	import InputBarControls from './InputBarControls.svelte';
	import InputBarFileInputs from './InputBarFileInputs.svelte';
	import {
		type Attachment,
		type SheetView,
		insertTextAtCaret,
		placeCaretAtEnd,
		resizeInputBar,
		uploadThreadAttachment,
	} from './inputBarHelpers';
	import { createComposerDrafts, sendAndRestoreDraft, type ComposerDraft } from './composerDrafts';
	import { composerCanSend, hasReadySwitchDestination as readySwitchDestination, type SendOutcome } from './composerSendReadiness';
	import { findModelById, loadModels, loadThreadCatalog } from '$lib/stores/models';
	import { sendMessage, stopTurn } from '$lib/ws';
	import { watchComposer } from './inputBarLifecycle';
	import { toasts } from '$lib/stores/toast';
	import { emitProductEvent } from '$lib/product_events';
	import { onModelSheetRequest } from '$lib/stores/modelSheetRequest';
	import { updateRestarting } from '$lib/update/store';

	import { corsiaAgentId } from '$lib/utils/conductor';
		let { threadId = undefined, onSend, onTriggerWarm, onAutoCreateThread }: {
			threadId?: string | null;
			onSend: () => void;
			onTriggerWarm?: () => void;
			onAutoCreateThread?: () => Promise<void>;
		} = $props();

		// Defaults to the active thread so the regular composer's behavior is
		// unchanged; an explicit id (the subagent leaf) overrides it (#1949).
		const boundThreadId = $derived(threadId ?? $activeThreadId);
		const isStreamingBoundThread = $derived(isThreadTurnActive($turnStates, boundThreadId));

		let isAutoCreating = $state(false);
	let threadCreationPromise: Promise<void> | null = null;

		let inputText = $state('');
		let inputEl = $state<HTMLDivElement | undefined>(undefined);
		let isComposing = false;
	let fileInputsRef: InputBarFileInputs;
	let pendingAttachments = $state<Attachment[]>([]);
	let comments = $state<ChatComment[]>([]);
	let commentSelection: ChatCommentSelection;
	let uploadError = $state('');
	let inputAreaEl = $state<HTMLDivElement | undefined>(undefined);
	let isMultiLine = $state(false);

		const isTouchDevice = typeof window !== 'undefined' && ('ontouchstart' in window || navigator.maxTouchPoints > 0);

		let sheetView = $state<SheetView>(null);
		let focusLocalOnly = $state(false);
		function toggleSheet(view: Exclude<SheetView, null>) { sheetView = sheetView === view ? null : view; }
		function closeSheet() { sheetView = null; }

		// The top composer owns the sheet state and focus request for every bound control.
		$effect(() => threadId === undefined ? onModelSheetRequest((request) => {
			focusLocalOnly = request.focusLocalOnly;
			if (sheetView !== 'model') { void loadModels({ refreshUsage: true }); sheetView = 'model'; }
		}) : undefined);

		const isMobile = typeof window !== 'undefined' && window.innerWidth <= 768;

		const hasImageAttachments = $derived(pendingAttachments.some(a => a.is_image));
		const selection = $derived($composerSelectionFor(threadId));
		const activeModel = $derived(findModelById(selection.models, selection.modelId));
		const catalogThreadId = $derived(!selection.isOpen ? selection.threadId : null); // another thread's composer reads its own catalog
		$effect(() => { if (catalogThreadId) void loadThreadCatalog(catalogThreadId); });
		$effect(() => {
		if (!sheetView) return;
		const onPointerDown = (e: PointerEvent) => {
			const t = e.target as HTMLElement | null;
			if (!t) return;
			if (typeof t.closest !== 'function') return;
			if (t.closest('.action-sheet')) return;
			if (t.closest('[data-sheet-trigger]')) return;
			closeSheet();
		};
		document.addEventListener('pointerdown', onPointerDown, true);
		return () => document.removeEventListener('pointerdown', onPointerDown, true);
	});

	const drafts = createComposerDrafts({
		current: currentComposerDraft,
		restore: restoreComposerDraft,
		reset: resetComposer,
		boundThreadId: () => boundThreadId,
		hasText: () => inputText.length > 0 || comments.length > 0,
	});

	$effect(() => drafts.switchThread(boundThreadId));

	async function handleFileSelect(files: FileList | null) {
		if (!files || files.length === 0) return;
		const t = $token;
		if (!t) return;
		uploadError = '';
		emitProductEvent('composer', 'attached', {
			thread_id: boundThreadId,
			file_count: files.length,
		});
		for (const file of Array.from(files)) {
			const { attachment: att, error } = await uploadThreadAttachment(
				file,
				t,
				() => boundThreadId,
					ensureThread,
			);
			if (error) uploadError = error;
			if (att) {
				pendingAttachments = [...pendingAttachments, att];
				emitProductEvent('attachment', 'added', {
					thread_id: boundThreadId,
					mime_type: att.mime_type,
					is_image: att.is_image,
				});
			}
		}
	}

	function removeAttachment(fileId: string) {
		const att = pendingAttachments.find(a => a.file_id === fileId);
		if (att?.preview_url) URL.revokeObjectURL(att.preview_url);
		pendingAttachments = pendingAttachments.filter(a => a.file_id !== fileId);
	}

	function prepareMessage() {
		if (!inputText.trim() && pendingAttachments.length === 0 && comments.length === 0) return null;
		if (!boundThreadId && !onAutoCreateThread) return null;
		const snapshot = () => boundThreadId ? ({
			threadId: boundThreadId,
			content: inputText.trim(),
			modelId: selection.modelId ?? undefined,
			attachments: [...pendingAttachments],
		}) : null;
		return boundThreadId ? snapshot() : ensureThread().then(snapshot);
	}

	function currentComposerDraft(): ComposerDraft | null {
		if (!inputText.trim() && pendingAttachments.length === 0 && comments.length === 0) return null;
		return {
			content: inputText,
			attachments: [...pendingAttachments],
			comments: [...comments],
		};
	}

	function restoreComposerDraft(draft: ComposerDraft | undefined): void {
		inputText = draft?.content ?? '';
		pendingAttachments = draft ? [...draft.attachments] : [];
		comments = draft?.comments ? [...draft.comments] : [];
		isMultiLine = false;
		if (inputEl) {
			inputEl.textContent = inputText;
			inputEl.style.height = 'auto';
			if (inputText) {
				autoResize(inputEl);
				placeCaretAtEnd(inputEl);
			}
		}
	}

	function resetComposer() {
		inputText = '';
		if (inputEl) inputEl.textContent = '';
		pendingAttachments = [];
		comments = [];
		uploadError = '';
		if (inputEl) inputEl.style.height = 'auto';
		isMultiLine = false;
	}

	type DeliveryIntent = 'apply_now' | 'queue_after_turn';

	async function handleSend(deliveryIntent: DeliveryIntent = 'apply_now'): Promise<SendOutcome> {
		if (!canSendToModel) return 'not_sent';
		const prepared = prepareMessage();
		const message = prepared instanceof Promise ? await prepared : prepared;
		if (!message || !canSendToModel) return 'not_sent';
		const { threadId, content, modelId, attachments } = message;
		const submittedDraft = currentComposerDraft() ?? {
			content,
			attachments: [...attachments],
		};
		emitProductEvent('composer', 'sent', {
			thread_id: threadId,
			char_count: content.length,
			attachment_count: attachments.length,
			...(deliveryIntent === 'queue_after_turn' ? { delivery_intent: deliveryIntent } : {}),
		});
		const payloadComments = comments.map(({ id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid }) => ({ id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid }));
		const inputId = sendMessage(threadId, content, modelId, attachments, undefined, deliveryIntent, payloadComments);
		if (!inputId) {
			toasts.show('Connection lost before CORSIA could accept this message.', 'error');
			return 'notified';
		}
		drafts.trackSubmit(inputId, threadId, submittedDraft);
		// Save before onSend, which may synchronously switch threads.
		drafts.saveCurrentDraft(threadId);
		resetComposer();
		onSend();
		return 'sent';
	}

	const activeThread = $derived(selection.thread);
	const hasReadySwitchDestination = $derived(readySwitchDestination(activeThread, selection.models));
	// A thread outside the list (a worker, the Corsia agent) may have no model to gate on: the server holds or routes it.
	const allowUnlistedNoModelThread = $derived(boundThreadId !== null && (boundThreadId === $corsiaAgentId || !selection.isOpen));
	const canSendToModel = $derived(!$updateRestarting && composerCanSend(activeThread, activeModel, selection.models, allowUnlistedNoModelThread));
	const hasMessageToSend = $derived(inputText.trim().length > 0 || pendingAttachments.length > 0 || comments.length > 0);

	function handleSendButtonClick() {
		if (inputText.trim() || pendingAttachments.length > 0 || comments.length > 0) {
			handleSend();
		} else if (isStreamingBoundThread && boundThreadId) {
			stopTurn(boundThreadId);
		}
	}

		async function ensureThread() {
			if (boundThreadId) return;
			if (!onAutoCreateThread) return;
			// If creation is already in flight, await that promise instead of returning early.
			// Without this, a concurrent handleSend() would see isAutoCreating=true, return
			// immediately, then hit !$activeThreadId and silently drop the message (#528).
			if (threadCreationPromise) {
				await threadCreationPromise;
				return;
			}
			isAutoCreating = true;
			threadCreationPromise = (async () => {
				try {
					await onAutoCreateThread();
				} catch (err) {
					console.error('[InputBar] onAutoCreateThread failed:', err);
				} finally {
					isAutoCreating = false;
					threadCreationPromise = null;
				}
			})();
			await threadCreationPromise;
		}

		function handleInput() {
			if (isComposing) return;
			inputText = inputEl?.textContent ?? '';
			if (inputEl) autoResize(inputEl);
			onTriggerWarm?.();
			// Do NOT call ensureThread() here — thread creation only on Send or attachment selection (#611).
		}

		function handleKeydown(e: KeyboardEvent) {
			if (e.key !== 'Enter' || e.shiftKey || isComposing || e.repeat) return;
			const queueAfterTurn = e.metaKey || e.ctrlKey;
			if (isTouchDevice && !queueAfterTurn) return;
			e.preventDefault();
			handleSend(queueAfterTurn ? 'queue_after_turn' : 'apply_now');
		}

	function handlePaste(e: ClipboardEvent) {
		if ($updateRestarting) return e.preventDefault();
		const files = e.clipboardData?.files;
		if (files && files.length > 0) {
			e.preventDefault();
			handleFileSelect(files);
			return;
		}
		const text = e.clipboardData?.getData('text/plain');
		if (text === undefined) return;
		e.preventDefault();
		insertTextAtCaret(inputEl, text);
		handleInput();
	}

	function autoResize(el: HTMLElement) {
		resizeInputBar(el, inputAreaEl, (value) => { isMultiLine = value; });
	}

	onMount(() => watchComposer(() => inputAreaEl, () => boundThreadId, drafts));

	export function getInputText() { return inputText; }
	export function getDrafts() { return drafts.threadDrafts; }
	export function setAndSend(text: string, preserveDraft = false): Promise<SendOutcome> {
		const draft = preserveDraft ? { content: inputText, attachments: [...pendingAttachments], comments: [...comments] } : null;
		const draftThreadId = boundThreadId;
		inputText = text;
		if (preserveDraft) { pendingAttachments = []; comments = []; }
		if (inputEl) inputEl.textContent = text;
		return sendAndRestoreDraft(draft, draftThreadId, tick, handleSend, restoreComposerDraft, drafts.saveCurrentDraft);
	}

	export async function setText(text: string) {
		inputText = text;
		pendingAttachments = [];
		comments = [];
		await setContentEditableText(inputEl, text, autoResize, placeCaretAtEnd);
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="input-floater"
	bind:this={inputAreaEl}
	onpointerdown={(e) => {
		const t = e.target as HTMLElement | null;
		if (!t) return;
		if (t.closest('.composer')) return;
		if (t.closest('textarea, input')) return;
		e.preventDefault();
	}}
	onmousedown={(e) => {
		const t = e.target as HTMLElement | null;
		if (!t) return;
		if (t.closest('.composer')) return;
		if (t.closest('textarea, input')) return;
		e.preventDefault();
	}}
>
	<InputBarFileInputs bind:this={fileInputsRef} onFiles={handleFileSelect} />
	<ChatCommentSelection bind:this={commentSelection} threadId={boundThreadId} {comments} surface={inputAreaEl} onChange={value => comments = value} />

	{#if uploadError}
		<div class="upload-error">{uploadError} <button onclick={() => uploadError = ''}>✕</button></div>
	{/if}

	<InputBarControls
		surface="sheets"
		threadId={boundThreadId}
		{sheetView}
		{toggleSheet}
		{isMobile}
		{hasImageAttachments}
		{inputText}
		pendingAttachmentCount={pendingAttachments.length}
		hasMessageToSend={hasMessageToSend}
		{canSendToModel}
		queueUntilSwitch={hasReadySwitchDestination}
		{focusLocalOnly}
		onfocusconsumed={() => focusLocalOnly = false}
		onAttachFile={() => fileInputsRef?.openFile()}
		onAttachCamera={() => fileInputsRef?.openCamera()}
		onAttachPhotos={() => fileInputsRef?.openPhotos()}
		onSendClick={handleSendButtonClick}
			onQueueAfterTurn={() => handleSend('queue_after_turn')}
	/>

	<InputBarComposer
		bind:inputEl
		threadId={boundThreadId}
		{pendingAttachments}
		{comments}
		onEditComment={id => void commentSelection?.edit(id)}
		onRemoveComment={id => comments = comments.filter(comment => comment.id !== id)}
		{isMultiLine}
		{sheetView}
		{toggleSheet}
		{isMobile}
		{hasImageAttachments}
		{inputText}
		hasMessageToSend={hasMessageToSend}
		{canSendToModel}
		queueUntilSwitch={hasReadySwitchDestination}
		onAttachFile={() => fileInputsRef?.openFile()}
		onAttachCamera={() => fileInputsRef?.openCamera()}
		onAttachPhotos={() => fileInputsRef?.openPhotos()}
		onSendClick={handleSendButtonClick}
			onQueueAfterTurn={() => handleSend('queue_after_turn')}
		{removeAttachment}
		{handleKeydown}
		{handleInput}
		handleFocus={() => onTriggerWarm?.()}
		{handlePaste}
		onCompositionStart={() => { isComposing = true; }}
		onCompositionEnd={() => { isComposing = false; handleInput(); }}
	/>
</div>
