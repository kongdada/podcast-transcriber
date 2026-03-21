import importlib.util
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "podcast_workflow.py"
SPEC = importlib.util.spec_from_file_location("podcast_workflow", MODULE_PATH)
pw = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = pw
SPEC.loader.exec_module(pw)


class PodcastWorkflowUnitTests(unittest.TestCase):
    def test_classify_source_show_vs_episode(self) -> None:
        show_info = {"_type": "playlist", "entries": [{"title": "A"}]}
        episode_info = {"title": "single"}

        self.assertEqual(pw.classify_source(show_info), "show")
        self.assertEqual(pw.classify_source(episode_info), "episode")

    def test_choose_episode_by_index(self) -> None:
        eps = [
            pw.EpisodeCandidate("ep1", "https://x/1", 1, 100, None, None),
            pw.EpisodeCandidate("ep2", "https://x/2", 2, 90, None, None),
            pw.EpisodeCandidate("ep3", "https://x/3", 3, 80, None, None),
        ]
        selected = pw.choose_episode(eps, 2)
        self.assertEqual(selected.title, "ep2")

    def test_choose_episode_interactive(self) -> None:
        eps = [
            pw.EpisodeCandidate("ep1", "https://x/1", 1, 100, None, None),
            pw.EpisodeCandidate("ep2", "https://x/2", 2, 90, None, None),
        ]
        with mock.patch.object(pw.sys.stdin, "isatty", return_value=True):
            selected = pw.choose_episode(eps, None, input_fn=lambda _: "1")
        self.assertEqual(selected.title, "ep1")

    def test_run_cmd_retries_once(self) -> None:
        fail = pw.subprocess.CompletedProcess(args=["cmd"], returncode=1, stdout="", stderr="failed")
        ok = pw.subprocess.CompletedProcess(args=["cmd"], returncode=0, stdout="ok", stderr="")

        with mock.patch.object(pw.subprocess, "run", side_effect=[fail, ok]) as run_mock:
            result = pw.run_cmd(["echo", "x"], retries=1, retry_wait_s=0)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run_mock.call_count, 2)

    def test_build_whisper_cmd_uses_validated_flags(self) -> None:
        cmd = pw.build_whisper_cmd(
            whisper_bin="./build/bin/whisper-cli",
            model_path="./models/ggml-large-v3-turbo.bin",
            audio_path=Path("/tmp/a.mp3"),
            out_prefix=Path("/tmp/01_transcript"),
            language="zh",
            threads=8,
        )
        self.assertNotIn("-ng", cmd)
        self.assertIn("-mc", cmd)
        self.assertIn("0", cmd)
        self.assertIn("-otxt", cmd)
        self.assertIn("-osrt", cmd)
        self.assertIn("-oj", cmd)
        self.assertIn("-pp", cmd)
        self.assertIn("-l", cmd)
        self.assertIn("zh", cmd)

        cpu_cmd = pw.build_whisper_cmd(
            whisper_bin="./build/bin/whisper-cli",
            model_path="./models/ggml-large-v3-turbo.bin",
            audio_path=Path("/tmp/a.mp3"),
            out_prefix=Path("/tmp/01_transcript"),
            language="zh",
            threads=8,
            use_gpu=False,
        )
        self.assertIn("-ng", cpu_cmd)


class PodcastWorkflowExecutionTests(unittest.TestCase):
    def _make_args(self, out_root: str, episode_index: int | None = None) -> Namespace:
        return Namespace(
            url="https://example.com/podcast",
            episode_index=episode_index,
            out_root=out_root,
            whisper_bin="./build/bin/whisper-cli",
            asr_model="./models/ggml-large-v3-turbo.bin",
            diarization=False,
            tdrz_model="./models/ggml-small.en-tdrz.bin",
            language="zh",
            threads=8,
            gpu=True,
            keep_awake=False,
            progress_interval=30,
            keep_json_artifacts=False,
            retries=1,
        )

    def test_mocked_integration_generates_download_and_transcript_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as out_root, tempfile.TemporaryDirectory() as fake_tmp:
            args = self._make_args(out_root, episode_index=1)
            src = Path(fake_tmp) / "source.webm"
            src.write_bytes(b"source")

            def fake_transcode(_ffmpeg, _src, dst, retries=None):
                Path(dst).write_bytes(b"mp3")

            def fake_run_whisper(
                _whisper_bin,
                *,
                model_path,
                audio_path,
                out_dir,
                language,
                threads,
                retries,
                use_gpu,
                progress_interval_s,
                expected_audio_ms,
                keep_awake,
            ):
                self.assertEqual(model_path, "./models/ggml-large-v3-turbo.bin")
                self.assertEqual(language, "zh")
                self.assertEqual(threads, 8)
                self.assertEqual(audio_path.name, "audio.mp3")
                self.assertTrue(use_gpu)
                self.assertEqual(progress_interval_s, 30)
                self.assertFalse(keep_awake)

                jsn = out_dir / "01_transcript.json"
                jsn.write_text("{}", encoding="utf-8")

                transcript_json = {
                    "result": {"language": "zh"},
                    "transcription": [
                        {"offsets": {"from": 0, "to": 1000}, "text": "你好，世界"},
                    ],
                }
                return pw.TranscriptionResult(
                    command=["whisper-cli", "-l", "zh", "-ng", "-mc", "0"],
                    transcript_json=transcript_json,
                )

            show_info = {
                "_type": "playlist",
                "entries": [
                    {
                        "title": "第1期",
                        "webpage_url": "https://example.com/ep1",
                        "playlist_index": 1,
                        "release_timestamp": 200,
                    }
                ],
            }

            with (
                mock.patch.object(pw, "preflight", return_value=("yt-dlp", "ffmpeg", "whisper-cli")),
                mock.patch.object(pw, "inspect_source", return_value=show_info),
                mock.patch.object(pw, "download_audio", return_value=src),
                mock.patch.object(pw, "transcode_to_mp3", side_effect=fake_transcode),
                mock.patch.object(pw, "run_whisper_transcription", side_effect=fake_run_whisper),
            ):
                out_dir = pw.execute_workflow(args)

            self.assertTrue((out_dir / "audio.mp3").exists())
            self.assertTrue((out_dir / "01_transcript.md").exists())
            self.assertFalse((out_dir / "01_transcript.txt").exists())
            self.assertFalse((out_dir / "01_transcript.srt").exists())
            self.assertFalse((out_dir / "01_transcript.json").exists())
            self.assertFalse((out_dir / "run_manifest.json").exists())

            transcript = (out_dir / "01_transcript.md").read_text(encoding="utf-8")
            self.assertIn("[00:00:00 - 00:00:01]", transcript)
            self.assertIn("你好，世界", transcript)

            # Final output is intentionally minimal by default.
            self.assertFalse((out_dir / "01_diarization_tdrz.json").exists())


if __name__ == "__main__":
    unittest.main()
