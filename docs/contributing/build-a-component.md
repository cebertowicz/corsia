# Build a frontend component

Use this guide to compose an existing design-system control in a Svelte component, then check its behavior and presentation. Read [the design system](../../frontend/DESIGN-SYSTEM.md) for the visual and accessibility contracts and [the frontend map](../../FRONTEND.md) for the code layout.

## Choose a control

Look in `frontend/src/lib/components/ui/` and at existing callers before adding a new control. The barrel `ui/index.ts` exports commonly used primitives; other controls can be imported from their individual files. The `/_design` route demonstrates the shared controls.

For example, `frontend/src/lib/components/SettingsUpdateOffer.svelte` uses `Button` for an update action. This smaller component follows the same import and callback pattern:

```svelte
<script lang="ts">
  import { Button } from '$lib/components/ui';

  let { busy = false, onRetry }: {
    busy?: boolean;
    onRetry: () => void;
  } = $props();
</script>

<Button size="sm" loading={busy} onclick={onRetry}>Retry</Button>
```

A parent supplies `onRetry` and owns `busy`. The button defaults to the secondary variant and `type="button"`. `loading` shows a spinner and disables the button. Its visible text supplies its accessible name. For an icon-only action, use `IconButton` and supply its required accessible label; check its props in `IconButton.svelte`. The `pill` and `chip` button variants are for setup.

For a form field, `Field` provides the layout and a label when `labelFor` matches the control's `id`. Supply the input and its state yourself. Inspect `Field.svelte` for its layouts and variants; connect any help or error text to the input when it must be announced.

## Fit the surface

Use the semantic tokens in `frontend/src/app.css` rather than inventing local colors. Follow the design system's guidance for focus, motion, themes, touch targets, and safe areas. Test the component inside its real surface at narrow and wide widths, including long text, keyboard focus, disabled and loading states, dark and light themes, and reduced motion where applicable. The shared button expands its touch target at narrow widths; check neighboring controls still have usable targets.

## Check it yourself

After installing dependencies with `npm ci` in `frontend/`, run the relevant focused component test and the Svelte check from the repository root:

```sh
cd frontend
npm run test -- src/lib/components/ui/Field.test.ts
npm run check
```

Replace the test path with the test for the component you changed. Both checks should pass. Run the app in the project's development environment and exercise the actual surface as well; a unit test alone does not check its layout or interaction in context.

Checked against the code at `de2e414bce808912cf9b297650277cd0711b4676`.
