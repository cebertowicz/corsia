# Rubric — Multi-turn dialog with tool use

The judge scores 0.0-1.0. Per-turn tool-call quality is the dominant
signal; final-state correctness is the gate that says the run actually
delivered the user's request.

## 1. Per-turn tool-call accuracy (weight 0.40)

For each of the three turns, score:

- Turn 1 (list): Did the agent call a directory-listing tool? Were the
  arguments sane (path is "." or the project root)? Were the results
  used in the assistant reply?
- Turn 2 (read): Did the agent pick a file the listing actually showed?
  Did it call read_file with that path? Did the assistant reply quote
  or summarize the read content (not hallucinate it)?
- Turn 3 (write): Did the agent call write_file with the requested
  filename (WORKFLOW-EVAL-SUMMARY.txt) and a 2-sentence body derived
  from the turn-2 read? Was the write confirmed to the user?

Average across the three turns. A turn with the wrong tool, hallucinated
arguments, or no tool call at all when one was needed earns 0.0 for
that turn.

## 2. Error recovery (weight 0.20)

If any tool call returned an error or an empty/unhelpful result, did
the agent:

- acknowledge it explicitly to the user, OR
- retry with adjusted arguments, OR
- pick a different tool sensibly?

A silent swallow (agent ignores a tool error and confabulates) is 0.0.
Clean runs with no errors earn full credit by default.

## 3. Final workspace state (weight 0.30)

The user asked for `WORKFLOW-EVAL-SUMMARY.txt` to be written with a
2-sentence summary of the README. Score:

- 1.0: A write_file call happened with that exact filename; the body is
  two sentences (≈30-200 chars); the body is grounded in the read.
- 0.5: Write happened but body is wrong length, or content drifts from
  the read.
- 0.0: No write happened, or write went to a different file.

## 4. Conversational quality (weight 0.10)

Did the assistant communicate intelligibly between tool calls? Did it
explain what it was doing? Pure tool-spam with no narration caps at 0.5.

## Output format

```
{"score": 0.78, "reason": "all 3 tool calls correct; write content slightly long but grounded in read"}
```

Use the weighted sum; round to two decimals; reason ≤ 200 chars.
