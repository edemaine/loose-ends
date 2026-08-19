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

    def test_open_problem_attempts_are_cumulative_snapshots(self):
        solve = (
            PROJECT_ROOT / "prompts" / "solve-open-problem.md"
        ).read_text(encoding="utf-8")
        review = (
            PROJECT_ROOT / "prompts" / "review-open-problem-attempt.md"
        ).read_text(encoding="utf-8")
        write = (
            PROJECT_ROOT / "prompts" / "write-open-problem-paper.md"
        ).read_text(encoding="utf-8")

        self.assertIn("new cumulative snapshot", solve)
        self.assertIn("prior_claim_dispositions", solve)
        self.assertIn("All top-level review fields describe the cumulative", review)
        self.assertIn("not on the best label ever assigned", review)
        self.assertIn("do not resurrect a superseded or\nrefuted claim", write)


if __name__ == "__main__":
    unittest.main()
