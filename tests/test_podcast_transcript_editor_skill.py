import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "codex-skills" / "podcast-transcript-editor" / "scripts" / "cleanup_helper.py"
SPEC = importlib.util.spec_from_file_location("cleanup_helper", MODULE_PATH)
ch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = ch
SPEC.loader.exec_module(ch)


TRANSCRIPT = (
    "# 转写稿\n\n"
    "- 生成时间: now\n"
    "- 来源链接: https://example.com\n"
    "- 集标题: 示例\n"
    "- 识别语言: zh\n\n"
    "## 正文\n\n"
    "张潇雨（00:00:00 - 00:00:10）：\n"
    "这是一段已经很干净的表达。\n\n"
    "雨白（00:00:10 - 00:00:40）：\n"
    "这个事情就是就是就是特别重要因为如果你不这么看你就很难理解整个结构它其实不是股价不是stock price而是那个。\n"
)


class CleanupHelperTests(unittest.TestCase):
    def test_analyze_clean_block_passes_through(self) -> None:
        block = "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。"
        result = ch.analyze_block(block)
        self.assertEqual(result["decision"], "pass_through")
        self.assertIn("clean_enough", result["reasons"])

    def test_analyze_dirty_block_needs_model(self) -> None:
        block = (
            "雨白（00:00:10 - 00:00:40）：\n"
            "这个事情就是就是就是特别重要因为如果你不这么看你就很难理解整个结构它其实不是股价不是stock price而是那个"
        )
        result = ch.analyze_block(block)
        self.assertEqual(result["decision"], "needs_model")
        self.assertGreaterEqual(result["dirty_score"], 2)
        self.assertTrue(any(reason in result["reasons"] for reason in ["repeated_phrase", "long_without_sentence_end"]))

    def test_build_plan_uses_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transcript_path = tmp / "01_transcript.md"
            transcript_path.write_text(TRANSCRIPT, encoding="utf-8")

            header, blocks = ch.split_transcript_markdown(TRANSCRIPT)
            dirty_block = blocks[1]
            key = ch.cache_key_for_block(dirty_block)
            cache_path = tmp / ch.DEFAULT_CACHE_FILENAME
            cache_data = {
                "version": ch.CACHE_VERSION,
                "prompt_id": ch.PROMPT_ID,
                "created_at": "now",
                "updated_at": "now",
                "entries": {
                    key: {
                        "block_hash": ch.block_hash(dirty_block),
                        "prompt_id": ch.PROMPT_ID,
                        "cleaned_block": dirty_block.replace("就是就是就是", "就是"),
                        "source_header": "雨白（00:00:10 - 00:00:40）：",
                        "model": "gpt-4.1",
                        "updated_at": "now",
                    }
                },
            }
            cache_path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")

            plan = ch.build_plan(transcript_path, cache_path=cache_path)
            self.assertEqual(plan["header_text"], header)
            self.assertEqual(plan["stats"]["from_cache"], 1)
            dirty = plan["blocks"][1]
            self.assertEqual(dirty["decision"], "from_cache")
            self.assertIsNotNone(dirty["cleaned_block"])

    def test_build_plan_marks_profile_hits_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transcript_path = tmp / "01_transcript.md"
            transcript_path.write_text(
                (
                    "# 转写稿\n\n- 生成时间: now\n- 来源链接: x\n- 集标题: 示例\n- 识别语言: zh\n\n"
                    "## 正文\n\n"
                    "雨白（00:00:00 - 00:00:10）：\n执行小酒馆这期节目我很喜欢。\n"
                ),
                encoding="utf-8",
            )
            profile_path = tmp / "demo.profile.json"
            profile_path.write_text(
                json.dumps({"replacements": {"执行小酒馆": "知行小酒馆"}}, ensure_ascii=False),
                encoding="utf-8",
            )

            plan = ch.build_plan(transcript_path, profile_path=profile_path)
            block = plan["blocks"][0]
            self.assertEqual(block["decision"], "needs_model")
            self.assertIn("profile_replacement_hits", block["reasons"])

    def test_assemble_writes_cleaned_output_and_updates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            cache_path = tmp / ch.DEFAULT_CACHE_FILENAME
            plan_path = tmp / "cleanup_plan.json"
            output_path = tmp / "02_transcript_clean.md"

            plan = {
                "version": ch.PLAN_VERSION,
                "created_at": "now",
                "prompt_id": ch.PROMPT_ID,
                "transcript_path": str(tmp / "01_transcript.md"),
                "cache_path": str(cache_path),
                "profile_path": None,
                "header_text": "# 转写稿\n\n- 生成时间: now\n- 来源链接: x\n- 集标题: 示例\n- 识别语言: zh\n\n## 正文\n\n",
                "stats": {"total_blocks": 2, "needs_model": 1, "pass_through": 1, "from_cache": 0},
                "blocks": [
                    {
                        "index": 1,
                        "cache_key": "aaa",
                        "decision": "pass_through",
                        "source_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "cleaned_block": None,
                        "header_line": "张潇雨（00:00:00 - 00:00:10）：",
                    },
                    {
                        "index": 2,
                        "cache_key": "bbb",
                        "decision": "needs_model",
                        "source_block": "雨白（00:00:10 - 00:00:40）：\n这个事情就是就是就是特别重要。",
                        "cleaned_block": "雨白（00:00:10 - 00:00:40）：\n这个事情就是特别重要。",
                        "header_line": "雨白（00:00:10 - 00:00:40）：",
                    },
                ],
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

            ch.assemble_from_plan(plan_path, output_path, model="gpt-4.1")

            rendered = output_path.read_text(encoding="utf-8")
            self.assertIn("# 转写稿（忠实清洗版）", rendered)
            self.assertIn("- 清洗方式: 忠实语义清洗", rendered)
            self.assertIn("这个事情就是特别重要。", rendered)

            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("bbb", cache["entries"])
            self.assertEqual(cache["entries"]["bbb"]["model"], "gpt-4.1")


if __name__ == "__main__":
    unittest.main()
