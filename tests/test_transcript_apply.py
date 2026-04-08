import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cacli.transcript_apply import (
    analyze_transcript,
    apply_actions_to_disk,
    generate_patch,
    run_transcript_apply,
)


class TranscriptApplyTests(unittest.TestCase):
    def _write_transcript(self, root: Path, name: str, entries: list[dict]) -> Path:
        path = root / name
        path.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n",
            encoding="utf-8",
        )
        return path

    def test_claude_analysis_and_patch_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hello.txt").write_text("hello\n", encoding="utf-8")
            transcript = self._write_transcript(
                root,
                "claude.jsonl",
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Write",
                                    "input": {
                                        "file_path": "new.txt",
                                        "content": "fresh\n",
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Edit",
                                    "input": {
                                        "file_path": "hello.txt",
                                        "old_string": "hello\n",
                                        "new_string": "hi\n",
                                    },
                                }
                            ]
                        },
                    },
                ],
            )

            analysis = analyze_transcript(str(transcript), cwd=str(root))

            self.assertEqual(analysis.provider, "claude")
            self.assertEqual(len(analysis.actions), 2)
            self.assertEqual(len(analysis.applicable_actions), 2)
            self.assertIsNone(analysis.blocker)

            patch_path = generate_patch(analysis, str(root / "recover.patch"))
            patch_text = patch_path.read_text(encoding="utf-8")
            self.assertIn("hello.txt", patch_text)
            self.assertIn("new.txt", patch_text)
            self.assertEqual((root / "hello.txt").read_text(encoding="utf-8"), "hello\n")

            apply_actions_to_disk(root, analysis.applicable_actions)
            self.assertEqual((root / "hello.txt").read_text(encoding="utf-8"), "hi\n")
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "fresh\n")

    def test_codex_tool_calls_with_stringified_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = self._write_transcript(
                root,
                "codex.jsonl",
                [
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "tool_call",
                            "name": "Write",
                            "arguments": json.dumps(
                                {"path": "notes.txt", "content": "a\n"}
                            ),
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "tool_call",
                            "name": "Edit",
                            "arguments": {
                                "path": "notes.txt",
                                "old_string": "a\n",
                                "new_string": "b\n",
                            },
                        },
                    },
                ],
            )

            analysis = analyze_transcript(
                str(transcript), cwd=str(root), provider="codex"
            )

            self.assertEqual(len(analysis.actions), 2)
            self.assertEqual(len(analysis.applicable_actions), 2)
            self.assertTrue(analysis.fully_applicable)

            apply_actions_to_disk(root, analysis.applicable_actions)
            self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "b\n")

    def test_unreconstructable_shell_command_blocks_later_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.txt").write_text("one\n", encoding="utf-8")
            transcript = self._write_transcript(
                root,
                "barrier.jsonl",
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Edit",
                                    "input": {
                                        "file_path": "data.txt",
                                        "old_string": "one\n",
                                        "new_string": "two\n",
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "sed -i 's/two/THREE/' data.txt"},
                                }
                            ]
                        },
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Edit",
                                    "input": {
                                        "file_path": "data.txt",
                                        "old_string": "THREE\n",
                                        "new_string": "four\n",
                                    },
                                }
                            ]
                        },
                    },
                ],
            )

            analysis = analyze_transcript(str(transcript), cwd=str(root))

            self.assertEqual(len(analysis.actions), 2)
            self.assertEqual(len(analysis.applicable_actions), 1)
            self.assertIsNotNone(analysis.blocker)
            self.assertEqual(len(analysis.bash_warnings), 1)
            self.assertIn("sed -i", analysis.bash_warnings[0].command)

            patch_path = generate_patch(analysis, str(root / "barrier.patch"))
            patch_text = patch_path.read_text(encoding="utf-8")
            self.assertIn("+two", patch_text)
            self.assertNotIn("+four", patch_text)

    def test_apply_patch_shell_command_is_reconstructed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foo.txt").write_text("hello\n", encoding="utf-8")
            transcript = self._write_transcript(
                root,
                "patch.jsonl",
                [
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "apply_patch <<'PATCH'\n"
                                "*** Begin Patch\n"
                                "*** Update File: foo.txt\n"
                                "@@\n"
                                "-hello\n"
                                "+goodbye\n"
                                "*** End Patch\n"
                                "PATCH"
                            ),
                        },
                    }
                ],
            )

            analysis = analyze_transcript(
                str(transcript), cwd=str(root), provider="codex"
            )
            self.assertEqual(len(analysis.actions), 1)
            self.assertEqual(len(analysis.applicable_actions), 1)
            apply_actions_to_disk(root, analysis.applicable_actions)
            self.assertEqual((root / "foo.txt").read_text(encoding="utf-8"), "goodbye\n")

    def test_auto_apply_mode_applies_clean_prefix_and_returns_failure_on_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.txt").write_text("alpha\n", encoding="utf-8")
            transcript = self._write_transcript(
                root,
                "auto.jsonl",
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Edit",
                                    "input": {
                                        "file_path": "app.txt",
                                        "old_string": "alpha\n",
                                        "new_string": "beta\n",
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "sed -i 's/beta/gamma/' app.txt"},
                                }
                            ]
                        },
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Edit",
                                    "input": {
                                        "file_path": "app.txt",
                                        "old_string": "gamma\n",
                                        "new_string": "delta\n",
                                    },
                                }
                            ]
                        },
                    },
                ],
            )

            exit_code = run_transcript_apply(
                SimpleNamespace(
                    transcript=str(transcript),
                    cwd=str(root),
                    provider="auto",
                    mode="auto-apply",
                    patch_file=None,
                )
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual((root / "app.txt").read_text(encoding="utf-8"), "beta\n")


if __name__ == "__main__":
    unittest.main()
