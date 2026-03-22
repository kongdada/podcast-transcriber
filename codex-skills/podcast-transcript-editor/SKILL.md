---
name: podcast-transcript-editor
description: Download and transcribe podcast episodes with the local podcast CLI, then produce a faithful cleaned transcript. Use when working on Apple Podcasts or Xiaoyuzhou links, or when given an existing `01_transcript.md` that should become a cleaner `02_transcript_clean.md` without changing meaning.
---

# Podcast Transcript Editor

## Overview

Use this skill to run the repository's local podcast workflow and then produce a faithful cleaned transcript. Keep `01_transcript.md` as the source of truth and write cleaned output to `02_transcript_clean.md` in the same directory.

## Token-Saving Workflow

### 1. Decide the input type

Treat the request as one of these two cases:

- Podcast URL: run the local CLI first, then clean the generated transcript
- Existing transcript path: read the existing `01_transcript.md`, then clean it directly

### 2. Run the local CLI for URL input

When the user provides a podcast URL, run from the repository root:

```bash
python3 scripts/podcast_workflow.py --url "<podcast-url>"
```

Add options only when the user has already supplied the information or the task clearly needs it:

- `--episode-index` for non-interactive show pages
- `--profile <name>` when a show-specific profile exists
- `--speaker-a-name` / `--speaker-b-name` for the common 2-speaker case
- `--speaker-name-map LABEL=NAME` for any extra explicit speaker labels
- `--no-diarization` only when the user explicitly wants it or the local diarization path is blocked

Do not over-claim automatic speaker attribution for `>2` person episodes. The current local `tinydiarize` path is still experimental and mainly useful for 2-speaker turn hints.

### 3. Build the cleanup plan first

Always run the helper before sending anything to the model:

```bash
python3 codex-skills/podcast-transcript-editor/scripts/cleanup_helper.py plan \
  "<path-to-01_transcript.md>" \
  --output "<path-to-cleanup-plan.json>"
```

If a matching podcast profile exists, pass it too:

```bash
python3 codex-skills/podcast-transcript-editor/scripts/cleanup_helper.py plan \
  "<path-to-01_transcript.md>" \
  --profile-file "scripts/podcast_profiles/<profile>.profile.json" \
  --output "<path-to-cleanup-plan.json>"
```

The plan classifies each block as:

- `pass_through`: skip the model entirely
- `needs_model`: send this block to the model
- `from_cache`: reuse a previous cleaned result

Before editing, read:

- [references/cleanup-standard.zh.md](references/cleanup-standard.zh.md)
- [references/cleanup-prompt.zh.txt](references/cleanup-prompt.zh.txt)
- [references/cache-format.md](references/cache-format.md)

### 4. Only send dirty blocks to the model

Never send the whole transcript by default.

Send only blocks where `decision == "needs_model"`.

Skip the model for blocks where:

- `decision == "pass_through"`
- `decision == "from_cache"`

For each dirty block, use the fixed prompt in [references/cleanup-prompt.zh.txt](references/cleanup-prompt.zh.txt). Keep the prompt short and stable so cache reuse remains effective.

### 5. Record cleaned blocks and assemble output

After the model returns cleaned text for each dirty block, write the cleaned block back into the plan JSON under that block's `cleaned_block` field.

Then assemble the final cleaned transcript:

```bash
python3 codex-skills/podcast-transcript-editor/scripts/cleanup_helper.py assemble \
  "<path-to-cleanup-plan.json>" \
  --output "<path-to-02_transcript_clean.md>" \
  --model "<model-name>"
```

This will:

- preserve `pass_through` blocks as-is
- reuse `from_cache` blocks
- write the final `02_transcript_clean.md`
- update `.podcast-transcript-editor-cache.json`

## Output Rules

Write `02_transcript_clean.md` as a cleaned transcript, not as notes about the cleanup.

Do:

- Keep the original title/header structure
- Keep speaker labels and time ranges
- Split long paragraphs where reading becomes tiring
- Remove obvious noise or duplicated junk only when confidence is high

Do not:

- Summarize
- Add interpretation
- Change claims or positions
- Merge separate turns into different speaker ownership
- Rewrite into a polished article
