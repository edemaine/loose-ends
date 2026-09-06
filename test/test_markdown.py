from pathlib import Path
import shutil
import subprocess
import unittest


class MarkdownRendererTests(unittest.TestCase):
    def test_node_suite(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Renderer tests require Node.js 22 or newer on PATH.")
        result = subprocess.run(
            [node, "--test", "test_markdown.cjs"],
            cwd=Path(__file__).resolve().parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            "Node renderer tests failed. Install dependencies with "
            "`pnpm --dir test install --frozen-lockfile`.\n\n" + result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
