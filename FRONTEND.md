# CORSIA — Frontend Instructions

## Component Map

```
frontend/src/
├── routes/+page.svelte      # Layout shell, WS events, session management
├── lib/
│   ├── components/          # 45+ components — see ls frontend/src/lib/components/
│   │   ├── Chat: Sidebar, MessageList, InputBar, AssistantMessage, UserMessageBubble, LiveToolActivity, Lightbox
│   │   ├── Pane/artifacts: Pane, PaneShell, ArtifactPane, EventChip, SubagentChip, SendRefChip
│   │   ├── Documents: DocumentsView, DocViewer, MarkdownEditor, DocCommentsPane, DocCommentThread, DocDetailPane
│   │   ├── Providers/settings: SettingsView, SettingsSystem, SettingsDebug, ProviderManager, ModelCard, ModelProfileEditor, v7 provider components
│   │   ├── Cost/system: CostDashboard, CostSummaryCards, CostDailyChart, CostBreakdownSection
│   │   └── Shell/project: HomeShell, HomePageView, HomeOverlays, ProjectSettingsView, ProjectRepos, CreateProjectModal, CommandPalette, ToastContainer
│   ├── stores/              # auth, sessions, documents, doc comments, providers, models, projects, costs, pane, theme, connection
│   ├── ws.ts                # WebSocket client — emits 'active_turns_restored' on auth_ok when active sessions exist (page refresh recovery)
│   └── markdown.ts          # Markdown renderer
```

## Design System

**Read `frontend/DESIGN-SYSTEM.md` before touching anything visual.**
It is the canonical spec for:

- **Tokens** in `app.css` (color, surface, radius, spacing, glass
  materials, motion, z-index, typography, theme overrides, accessibility,
  Tauri / PWA contracts).
- **Primitives** in `frontend/src/lib/components/ui/`: `Modal`, `Button`,
  `IconButton`, `Field`, `StatePill`, `Section`, `Message`. Demonstrated
  at `/_design`.
- **Surface composition rules** (sidebars, headers, drawers, modals,
  floating chips, overlays).
- **Anti-patterns** (glass over content, glass-in-glass, hardcoded blurs,
  custom buttons / fields / pills).
- **Audit appendix (§8)** — the punch list for ongoing refinements
  (glass token cleanup, drawer harmonization, Settings glass adoption,
  model-selector overhaul). Future implementation roadmap is §10.

Execution history of how this system came together lives in
the workspace, under `shared/`.

## Svelte 5

Use `$props()`, `$state()`, `$derived()`, `$effect()`. Do NOT use Svelte 4 syntax. **No `createEventDispatcher`** — use callback props instead (e.g., `onSelect?: (id: string) => void` in `$props()`). Mixing Svelte 4 event dispatch with Svelte 5 prop passing silently fails — the parent never receives the event.

## CSS Variable Convention

All color/theme values must be defined as CSS variables in `frontend/src/app.css` under both `:root` (dark mode defaults) and `[data-theme="light"]`. Do NOT use hex or rgba fallbacks in `var()` calls inside `.svelte` files (e.g., `var(--bg-surface, #fff)` is wrong — define `--bg-surface` in `app.css` instead). Numeric fallbacks are fine (e.g., `var(--radius-xs, 4px)`).
