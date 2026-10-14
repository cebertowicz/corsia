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

This section supersedes the capture-boundary note above for the final local correction. Candidate: this local commit atop `69e13da9`.

| Changed state | Final desktop-light | Final desktop-dark | Final mobile-light | Final mobile-dark |
| --- | --- | --- | --- | --- |
| Settings, automatic updates off — no `Installs at …` claim | `settings-off-desktop-light.png` | `settings-off-desktop-dark.png` | `settings-off-mobile-light.png` | `settings-off-mobile-dark.png` |
| Settings, automatic updates on — selected `03:30` time remains visible | `settings-on-desktop-light.png` | `settings-on-desktop-dark.png` | `settings-on-mobile-light.png` | `settings-on-mobile-dark.png` |
| Choice sheet — drain paragraph plus all decision actions | `sheet-drain-cue-desktop-light.png` | `sheet-drain-cue-desktop-dark.png` | `sheet-drain-cue-mobile-light.png` | `sheet-drain-cue-mobile-dark.png` |
| Failed schedule save — retry toast visible while the selected time stays `03:30` | `retry-toast-desktop-light.png` | `retry-toast-desktop-dark.png` | `retry-toast-mobile-light.png` | `retry-toast-mobile-dark.png` |

The final screenshots are in `frontend/test-results/flows-scheduled-updates-pa-14dbd--retry-and-drain-cue-matrix-chromium-desktop/`; `file` verified desktop frames at 1440×1000 and mobile frames at 393×852. Chromium-mobile produced the same 16 names in its sibling directory. The final fast path is the off/on Settings states, `sheet-drain-cue-mobile-dark.png`, and one retry frame; all were captured from the same run.

The 393 choice surface is the existing bottom-anchored `AdaptiveDrawer` (`data-adaptive-drawer`), not the desktop `Modal`. Its one scroll container is the choice content; the action strip is sticky within it. The browser test asserts that the drain block has no internal clipping, has at least 8 px spare room inside the drawer panel, and that its Tonight/Update-now action strip is within the panel. The drawer header supplies Later. No animation primitive changed: desktop continues through `Modal` and 393 continues through `AdaptiveDrawer`; the final videos record open and close by ×, Later, scrim, and Escape at `frontend/test-results/flows-scheduled-updates-pa-f7396-tion-for-every-dismiss-path-chromium-desktop/video.webm` and its chromium-mobile sibling.

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
