# CORSIA frontend

This map points contributors to the frontend's composition, shared controls, and conventions. For a worked example, see [Build a component](docs/contributing/build-a-component.md).

## Component Map

- `frontend/src/routes/+page.svelte` is the application entry and composition root; `+layout.svelte` supplies the layout.
- `frontend/src/lib/components/` contains the application surfaces and their components. Shared controls are in `components/ui/`; `components/attention/` contains the attention dock and its rows.
- `frontend/src/lib/stores/` holds shared client state, including the attention queue and answer state. `frontend/src/lib/api/` contains API clients.
- `frontend/src/lib/ws/` handles WebSocket transport and events. `frontend/src/lib/cm/` contains CodeMirror editor extensions.
- `frontend/src/routes/_design/` demonstrates shared controls at `/_design`.

## Design System

Read [the design system](frontend/DESIGN-SYSTEM.md) before visual work. It describes the chat reference screen, tokens, existing primitives, accessibility, mobile contracts, and surface composition. Theme tokens live in `frontend/src/app.css`; shared control implementations live in `frontend/src/lib/components/ui/`. The barrel `ui/index.ts` exports commonly used primitives, including `Button`, `Field`, `Modal`, `IconButton`, `Message`, `Section`, `StatePill`, and `Toggle`.

Use the existing semantic tokens and controls for new work. Check the implementation and actual callers for supported props and variants: not every component in `ui/` is exported by the barrel. The design system's section 10 is its design review rule.

## Svelte 5

For new Svelte 5 components, use runes such as `$props()`, `$state()`, and `$derived()`, and pass callback props for component events.

## CSS Variable Convention

Use semantic theme variables from `frontend/src/app.css` instead of hardcoded color fallbacks in component styles. Check dark and light themes, keyboard access, reduced motion, and narrow layouts for the surface being changed.

## Checks

After installing dependencies with `npm ci` in `frontend/`, `npm run check` runs Svelte and TypeScript checks; `npm run test -- <test-file>` runs a focused Vitest test. `npm run test` runs the frontend unit suite. See `frontend/package.json` for the current scripts.

Checked against the code at `7a8c55d84c75cbee7b8f26c8839f188fa466b559`.
