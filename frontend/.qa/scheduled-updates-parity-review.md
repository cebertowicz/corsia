# Scheduled-update parity evidence review

Status: full 16-frame evidence complete; visual sign-off remains for Product Experience.
Reviewed: 2026-10-14 PDT
Candidate: `e65175b9`
Capture boundary: the frames were captured pre-`e93ad7ba`. The local train refresh through `ca0cd15e` was retested; no captured state changed, so no frame was recaptured.

## Gap resolved: 16 explicit state × viewport × theme frames

All paths below are relative to `frontend/test-results/flows-scheduled-updates-fr-0bbd7-sktop-mobile-and-light-dark-chromium-desktop/`.

| Required state | 1440 light | 1440 dark | 393 light | 393 dark |
| --- | --- | --- | --- | --- |
| Settings, automatic updates off | `settings-off-desktop-light.png` | `settings-off-desktop-dark.png` | `settings-off-mobile-light.png` | `settings-off-mobile-dark.png` |
| Settings, automatic updates on, selected time | `settings-on-desktop-light.png` | `settings-on-desktop-dark.png` | `settings-on-mobile-light.png` | `settings-on-mobile-dark.png` |
| Update choice sheet: Automatic plus drain cue | `sheet-drain-cue-desktop-light.png` | `sheet-drain-cue-desktop-dark.png` | `sheet-drain-cue-mobile-light.png` | `sheet-drain-cue-mobile-dark.png` |
| Failed schedule save / retry toast | `retry-toast-desktop-light.png` | `retry-toast-desktop-dark.png` | `retry-toast-mobile-light.png` | `retry-toast-mobile-dark.png` |

The Chromium-mobile project produced the same 16 names in its sibling output directory. The test results directory is intentionally overwritten by the next E2E run.

## Reproduction and proof

`CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1 CORSIA_E2E_MOCK=1 CORSIA_E2E_BACKEND_PORT=3120 CORSIA_E2E_FRONTEND_PORT=5181 bash scripts/e2e-local --grep 'scheduled update full frame matrix'`

- Passed on `chromium-desktop` and `chromium-mobile`, without retries.
- Each state explicitly asserts its decisive content before screenshot: schedule label and selected time; Automatic plus drain cue in the choice sheet; failed-save retry toast.
- `file` verified all 16 documented desktop-project PNGs: desktop files are 1440×1000 and mobile files are 393×852.

## Design-review self-check

Gate 0: correction within the already approved Settings → System → Updates journey. These are whole-screen frames; no new product control, state owner, or interaction pattern was introduced.

1. Job and journey — N/A for the approved-behavior correction.
2. Placement and IA — pass by source and route: update controls remain in the existing Updates setting and its existing `UpdateJourney` modal/drawer.
3. Consistency — pass by source: existing `Modal`, `AdaptiveDrawer`, `Button`, and `Toggle` primitives are reused.
4. Space and craft — full frames are captured for Product Experience inspection; this local self-check does not claim a pixel-level visual sign-off.
5. Robustness — all four responsive/theme combinations complete; the update sheet/drawer has an accessible `Update CORSIA` label and the schedule toggle retains its accessible name.
6. Magic — automatic-update choice and its scheduled time remain visible at the point of the update decision.

Train-refresh impact check (2026-10-14): #623 moves outcome acknowledgement to the operation row (`outcome_seen_at`) and #631 hides only failures resolved by restart activation. The captured states are Settings off, Settings on, the update choice with its drain cue, and a schedule-save retry toast; none carries an activated restart failure or a stored acknowledgement. Focused frontend and backend tests passed on `e65175b9`, so the existing 16 frames remain valid without recapture. No browser-storage acknowledgement path was added.

## 2026-10-14 PE correction: final local evidence

This section supersedes the capture-boundary note above for the final local correction. Evidence was captured from the worktree based on `933df8ba` with the final duration-copy change applied.

### Final frame inventory

The final 16-frame inventory is at `frontend/test-results/flows-scheduled-updates-pa-14dbd--retry-and-drain-cue-matrix-chromium-desktop/`; Chromium-mobile has the same named inventory at `frontend/test-results/flows-scheduled-updates-pa-14dbd--retry-and-drain-cue-matrix-chromium-mobile/`. `file` verified the Chromium-desktop project's desktop-named frames at 1440×1000 and mobile-named frames at 393×852; Chromium-mobile's device-scaled counterparts are 3780×2625 and 1032×2237, respectively.

| Changed state | Final desktop-light | Final desktop-dark | Final mobile-light | Final mobile-dark |
| --- | --- | --- | --- | --- |
| Settings, automatic updates off — no `Installs at …` claim | `settings-off-desktop-light.png` | `settings-off-desktop-dark.png` | `settings-off-mobile-light.png` | `settings-off-mobile-dark.png` |
| Settings, automatic updates on — selected `03:30` time remains visible | `settings-on-desktop-light.png` | `settings-on-desktop-dark.png` | `settings-on-mobile-light.png` | `settings-on-mobile-dark.png` |
| Choice sheet — one duration statement plus all decision actions | `sheet-drain-cue-desktop-light.png` | `sheet-drain-cue-desktop-dark.png` | `sheet-drain-cue-mobile-light.png` | `sheet-drain-cue-mobile-dark.png` |
| Failed schedule save — retry toast visible while the selected time stays `03:30` | `retry-toast-desktop-light.png` | `retry-toast-desktop-dark.png` | `retry-toast-mobile-light.png` | `retry-toast-mobile-dark.png` |

The 393 choice surface is the existing bottom-anchored `AdaptiveDrawer` (`data-adaptive-drawer`), not the desktop `Modal`. Its one scroll container is the choice content; the action strip is sticky within it. The browser test asserts that the drain block has no internal clipping, has at least 8 px spare room inside the drawer panel, and that its Tonight/Update-now action strip is within the panel. The drawer header supplies Later. No animation primitive changed: desktop continues through `Modal` and 393 continues through `AdaptiveDrawer`.

### Attributable open/close recordings

Each recording below contains one `Update` opening followed by exactly the named close action; the test awaits both visible and hidden states. Desktop and mobile are separate recordings. The listed `00:00.000–…` span is the complete, inspectable path — there is no shared sequence or ambiguous second dismissal.

| Close action | Desktop recording and inspection span | Mobile recording and inspection span |
| --- | --- | --- |
| × | `frontend/test-results/flows-scheduled-updates-pa-1c046--close-motion-through-close-chromium-desktop/video.webm` — `00:00.000–00:02.600` | `frontend/test-results/flows-scheduled-updates-pa-1c046--close-motion-through-close-chromium-mobile/video.webm` — `00:00.000–00:02.640` |
| Later | `frontend/test-results/flows-scheduled-updates-pa-a4a2c--close-motion-through-later-chromium-desktop/video.webm` — `00:00.000–00:02.240` | `frontend/test-results/flows-scheduled-updates-pa-a4a2c--close-motion-through-later-chromium-mobile/video.webm` — `00:00.000–00:02.680` |
| Scrim | `frontend/test-results/flows-scheduled-updates-pa-e284b--close-motion-through-scrim-chromium-desktop/video.webm` — `00:00.000–00:01.920` | `frontend/test-results/flows-scheduled-updates-pa-e284b--close-motion-through-scrim-chromium-mobile/video.webm` — `00:00.000–00:02.040` |
| Escape | `frontend/test-results/flows-scheduled-updates-pa-02dd7-close-motion-through-escape-chromium-desktop/video.webm` — `00:00.000–00:01.640` | `frontend/test-results/flows-scheduled-updates-pa-02dd7-close-motion-through-escape-chromium-mobile/video.webm` — `00:00.000–00:01.960` |

Run command: `CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1 CORSIA_E2E_MOCK=1 CORSIA_E2E_BACKEND_PORT=3417 CORSIA_E2E_FRONTEND_PORT=5279 bash scripts/e2e-local --grep 'scheduled updates parity'`. All ten desktop/mobile matrix and motion tests passed without retries.

### Design-review regression self-check

1. Job and journey — N/A: approved behavior correction only.
2. Placement and IA — pass: Settings › System › Updates remains the only edit home.
3. Consistency — pass: existing `Modal`, `AdaptiveDrawer`, `Button`, and `Toggle` patterns remain in use.
4. Space and craft — pass in the 16 whole-screen frames; no clipped drain text or hidden mobile action strip in the geometry assertion.
5. Robustness — pass: 393 uses the drawer; its reachable actions and contained drain paragraph are asserted, and all four state × viewport × theme cells keep their truthful schedule state.
6. Magic — excluded: a product change, not part of this correction.

### PE flag dispositions

| Flag | Disposition |
| --- | --- |
| Duplicate facts | Off-state `Installs at …` in Settings and the choice sheet differs from the approved round-12 state truth; fixed by rendering it only while automatic is on. |
| Unneeded journey copy | Beyond the approved round-12 copy; excluded. |
| Desktop layer separation | Desktop `Modal` is the approved round-12 surface; excluded. |
| Duplicate exits | The existing ×/Later/scrim/Escape exits are approved round-12 behavior; excluded, but their motion is now recorded. |
| Nesting | No nesting changed by this correction; the approved existing entry path is excluded. |
