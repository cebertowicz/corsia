<script lang="ts">
	import './InputBar.css';
	import { tick, onMount } from 'svelte';
	import { setContentEditableText } from '$lib/inputText';
	import { token } from '$lib/stores/auth';
	import { activeThreadId, isThreadTurnActive, threads } from '$lib/stores/threads';
	import { turnStates } from '$lib/ws/turnState';
	import InputBarComposer from './InputBarComposer.svelte';
	import InputBarControls from './InputBarControls.svelte';
	import InputBarFileInputs from './InputBarFileInputs.svelte';
	import {
		type Attachment,
		type SheetView,
		insertTextAtCaret,
		placeCaretAtEnd,
		resizeInputBar,
		uploadThreadAttachment,
		watchInputBarHeight,
	} from './inputBarHelpers';
	import { createComposerDrafts, sendAndRestoreDraft, type ComposerDraft } from './composerDrafts';
	import { composerCanSend, hasReadySwitchDestination as readySwitchDestination, type SendOutcome } from './composerSendReadiness';
	import { activeModelId, findModelById, loadModels, models } from '$lib/stores/models';
	import { on, sendMessage, stopTurn } from '$lib/ws';
	import { subagentMeta } from '$lib/stores/subagents';
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
		const activeModel = $derived(findModelById($models, $activeModelId));
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
		hasText: () => inputText.length > 0,
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
		if (!inputText.trim() && pendingAttachments.length === 0) return null;
		if (!boundThreadId && !onAutoCreateThread) return null;
		const snapshot = () => boundThreadId ? ({
			threadId: boundThreadId,
			content: inputText.trim(),
			modelId: $activeModelId ?? undefined,
			attachments: [...pendingAttachments],
		}) : null;
		return boundThreadId ? snapshot() : ensureThread().then(snapshot);
	}

	function currentComposerDraft(): ComposerDraft | null {
		if (!inputText.trim() && pendingAttachments.length === 0) return null;
		return {
			content: inputText,
			attachments: [...pendingAttachments],
		};
	}

	function restoreComposerDraft(draft: ComposerDraft | undefined): void {
		inputText = draft?.content ?? '';
		pendingAttachments = draft ? [...draft.attachments] : [];
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
		const inputId = deliveryIntent === 'queue_after_turn'
			? sendMessage(threadId, content, modelId, attachments, undefined, deliveryIntent)
			: sendMessage(threadId, content, modelId, attachments, undefined);
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

	const activeThread = $derived(
		$threads.find(thread => thread.id === boundThreadId),
	);
	const isSubagentThread = $derived(
		boundThreadId ? $subagentMeta.has(boundThreadId) : false,
	);
	const hasReadySwitchDestination = $derived(readySwitchDestination(activeThread, $models));
	const canSendToModel = $derived(!$updateRestarting && (isSubagentThread || composerCanSend(activeThread, activeModel, $models, boundThreadId !== null && boundThreadId === $corsiaAgentId)));
	const hasMessageToSend = $derived(inputText.trim().length > 0 || pendingAttachments.length > 0);

	function handleSendButtonClick() {
		if (inputText.trim() || pendingAttachments.length > 0) {
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

	onMount(() => {
		const stopWatchingHeight = watchInputBarHeight(
			() => inputAreaEl,
			(fn) => { void tick().then(fn); },
		);
		const unsubscribeMessage = on('message_added', data => drafts.settlePendingSubmit(data));
		const unsubscribeQueued = on('input_queued', data => drafts.settlePendingSubmit(data, 'queued'));
		const unsubscribeAccepted = on('input_accepted', data => drafts.settlePendingSubmit(data, 'delivered'));
		const unsubscribeError = on('error', data => drafts.settlePendingSubmit(data, 'failed'));
		// Every Stop gets an answer the user can read. "Stop worked" and "Stop is
		// broken" looked identical because the only signal was a control that
		// kept being offered (USER-REPORTS #6b) — and the outcome names are ours,
		// not theirs, so none of them reaches the screen.
		const unsubscribeStopReceipt = on('stop_receipt', data => {
			if (data?.thread_id !== boundThreadId) return;
			const receiptText: string | undefined = {
				stopped: 'Turn stopped',
				already_terminal: 'That turn had already finished',
				stale_active: 'That turn had already ended — a newer one is running',
				no_active_turn: 'Nothing was running to stop',
			}[data?.outcome as string];
			if (receiptText) toasts.show(receiptText, data.outcome === 'stopped' ? 'success' : 'info');
		});
		return () => {
			stopWatchingHeight();
			unsubscribeMessage();
			unsubscribeQueued();
			unsubscribeAccepted();
			unsubscribeError();
			unsubscribeStopReceipt();
		};
	});

	export function getInputText() { return inputText; }
	export function getDrafts() { return drafts.threadDrafts; }
	export function setAndSend(text: string, preserveDraft = false): Promise<SendOutcome> {
		const draft = preserveDraft ? { content: inputText, attachments: [...pendingAttachments] } : null;
		const draftThreadId = boundThreadId;
		inputText = text;
		if (preserveDraft) pendingAttachments = [];
		if (inputEl) inputEl.textContent = text;
		return sendAndRestoreDraft(draft, draftThreadId, tick, handleSend, restoreComposerDraft, drafts.saveCurrentDraft);
	}

	export async function setText(text: string) {
		inputText = text;
		pendingAttachments = [];
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
