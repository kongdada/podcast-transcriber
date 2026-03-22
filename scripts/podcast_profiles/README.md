# Podcast Profiles

`podcast_workflow.py` supports optional per-show or per-episode profiles.

Use profiles for deterministic rules only:

- fixed speaker display names
- known noise phrases
- known glossary replacements

Recommended file pattern:

- `*.profile.json`

Files beginning with `_` are ignored by auto-detection.

Matching fields are optional, but auto-detection only works when at least one matcher is provided:

- `input_url_regex`
- `source_url_regex`
- `title_regex`

Example:

```json
{
  "name": "demo",
  "match": {
    "title_regex": "知行小酒馆"
  },
  "speaker_a_name": "雨白",
  "speaker_b_name": "张潇雨",
  "noise_phrases": [
    "请不吝点赞",
    "打赏支持明镜"
  ],
  "replacements": {
    "博客": "播客",
    "执行小酒馆": "知行小酒馆"
  }
}
```

Notes:

- `speaker_a_name` / `speaker_b_name` cover the current 2-speaker workflow.
- Current local `tinydiarize` integration is still experimental and mainly useful for 2-speaker turn hints.

Use explicit profile selection when auto-detection is not enough:

```bash
python3 scripts/podcast_workflow.py \
  --url "<podcast-url>" \
  --profile my-show
```
