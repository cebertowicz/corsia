<!-- PREPARATION ONLY: fixture, never mounted/imported here.
Future destination: frontend/src/lib/__tests__/fixtures/FocusProofSurface.svelte.
The actual composed MessageList owns shaping, delivery regions, cards and rendering. -->
<script lang="ts">
  import MessageList from '$lib/components/MessageList.svelte';
  import type { TurnSummary } from '$lib/components/transcriptRunLabel';
  let { summaries, historyComplete = true, onLoadOlder = () => {} }: {
    summaries: Record<string, TurnSummary>;
    historyComplete?: boolean;
    onLoadOlder?: (needsMore?: () => boolean) => void;
  } = $props();
</script>
<MessageList messagesEl={undefined} userIsNearBottom={true} ignoreScrollEvents={false}
  onOpenLightbox={() => {}} turnSummaries={summaries} {historyComplete}
  hasMoreMessages={!historyComplete} {onLoadOlder} />
