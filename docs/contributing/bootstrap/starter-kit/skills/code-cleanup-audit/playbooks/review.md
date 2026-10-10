# Playbook: review

The final step after the eight tracks complete. You act as a skeptical senior engineer reviewing an AI-produced cleanup diff and finding-set. Your job is to **increase the quality of what's reported, not add new findings**.

## What you do

1. **Read the aggregated findings from all eight tracks.**
2. **Read any implemented diffs.** Verify each one is genuinely HIGH confidence + LOW risk.
3. **Apply the shape-vs-function lens holistically.** Three separate findings from three different tracks might actually be one systemic issue. Consolidate where appropriate.
4. **Demote over-eager findings.** If a track reported a `HIGH` that on review looks like `MEDIUM` or noise, demote it. False positives erode trust.
5. **Promote under-stated findings.** If a `MEDIUM` finding is actually critical (e.g., silent catch in a payment flow), promote it.
6. **Identify load-bearing fossils.** Some patterns look like cruft but exist to handle a real edge case no active test covers. Add these to the "explicit non-findings" section of the output so a future audit doesn't re-flag them.
7. **Write the executive summary.** 150 words max, highest-impact findings, no fluff.

## Questions to apply during review

For each implemented change:
- Does the diff pass the project's tests without modification? (If the skill didn't run tests, say so.)
- Does removing this break any public surface area? (Check `__all__`, exported types, published-package status.)
- Is the rationale written in terms a human could verify from just the file context, or does it require holding the whole audit in mind?

For each reported finding:
- Is the confidence tier accurate given the evidence cited?
- Does the suggested fix direction respect the layer boundaries of the codebase?
- Is this a symptom of a broader issue worth consolidating with other findings?

For consolidation (the shape-vs-function lens at the review level):
- Are multiple tracks' findings all pointing at the same underlying architectural issue? Combine them.
- Is a cluster of "dead" and "duplicated" code actually a single module that should be deleted? Promote to one big finding.
- Is a "weak type" + "silent error" combination actually one capability-bool-hiding-real-structure issue? Merge.

## Rules

- **Do not add new findings in review.** Your job is quality control, not discovery. Discovery happens in the tracks.
- **Do not implement new changes in review.** Only verify / demote / promote / consolidate existing ones.
- **Write the final report.** This is the deliverable; it reflects both the findings and your review judgments.
- **Name the checks you actually ran.** If you inspected diffs by reading them but didn't run the test suite, say so in the summary.

## Output

Assemble the final report per the format in the main SKILL.md. Save to `./.cleanup-audit/<ISO-date>-audit.md`.

The summary section at the top must include:
- Total findings by track.
- Implemented vs. reported counts.
- Top-3 highest-impact findings by your review judgment.
- Any patterns you saw across multiple tracks (the shape-vs-function angle).
- Calibration notes: things where the track was over- or under-eager, so future runs improve.

End the review by reporting back with the path to the saved report and the 150-word summary.
