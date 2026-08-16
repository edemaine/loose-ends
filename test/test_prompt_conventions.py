from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_PROMPTS = (
    "analyze-paper.md",
    "triage-open-problems.md",
    "search-open-problem-literature.md",
    "solve-open-problem.md",
    "review-open-problem-attempt.md",
    "review-open-problem-paper.md",
    "write-open-problem-paper.md",
)
MARKDOWN_MATH_INSTRUCTION = (
    "In every Markdown file, delimit all mathematical notation explicitly. Use\n"
    "`\\(...\\)` for inline mathematics and `\\[...\\]` for display mathematics."
)


class PromptConventionTests(unittest.TestCase):
    def test_markdown_prompts_require_explicit_math_delimiters(self):
        for filename in MARKDOWN_PROMPTS:
            with self.subTest(prompt=filename):
                prompt = (PROJECT_ROOT / "prompts" / filename).read_text(
                    encoding="utf-8"
                )
                self.assertIn(MARKDOWN_MATH_INSTRUCTION, prompt)


if __name__ == "__main__":
    unittest.main()
