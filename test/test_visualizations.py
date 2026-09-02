import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from validation import visualization as visualization_validation
from validation import visualization_review as review_validation
import codex_cli
import visualize_result
import visualizations


def generated_result(files=None):
    return {
        "status": "complete",
        "title": "A construction",
        "summary": "Plays the construction through one running example.",
        "entry_point": "visualization/index.html",
        "claim_refs": ["C-001"],
        "concepts": ["constructive proof", "running example"],
        "limitations": ["The coordinates are illustrative."],
        "verification_checks": [
            {
                "name": "Boundary case",
                "method": "Selected the boundary preset.",
                "result": "passed",
                "details": "The construction remains defined.",
            }
        ],
        "files": files or [
            "visualization/index.html",
            "visualization/app.js",
            "visualization/verification.md",
        ],
        "warnings": [],
    }


class VisualizationValidationTests(unittest.TestCase):
    def test_prompts_require_a_visible_standalone_exposition(self):
        root = Path(__file__).resolve().parents[1]
        author = (root / "prompts" / "visualize-result.md").read_text(
            encoding="utf-8"
        )
        reviewer = (root / "prompts" / "review-visualization.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("State the result", author)
        self.assertIn("Explain the proof or argument", author)
        self.assertIn("top to bottom", author)
        self.assertIn("professional mathematician", author)
        self.assertIn("Theorem", author)
        self.assertIn("Preserve theorem, proposition, lemma", author)
        self.assertIn("assertion currently being proved", author)
        self.assertIn("open-ended playground", author)
        self.assertIn("not_self_contained", reviewer)
        self.assertIn("hidden central result statement", reviewer)
        self.assertIn("intended audience is professional mathematicians", reviewer)
        self.assertIn("source theorem and lemma numbers", reviewer)
        self.assertIn("canned examples", reviewer)

    def test_reviewer_inherits_each_unspecified_model_setting(self):
        inherited = visualize_result._inherit_review_options(
            codex_cli.ModelOptions("primary", "xhigh", True),
            codex_cli.ModelOptions("critic", None, False),
        )

        self.assertEqual(inherited.model, "critic")
        self.assertEqual(inherited.reasoning_effort, "xhigh")
        self.assertTrue(inherited.fast)

    def test_accepts_free_form_static_app_with_known_claim(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = workspace / "visualization"
            directory.mkdir()
            (directory / "index.html").write_text(
                "<!doctype html><title>Construction</title><script src='app.js'></script>",
                encoding="utf-8",
            )
            (directory / "app.js").write_text("document.body.append('ok')", encoding="utf-8")
            (directory / "verification.md").write_text(
                "# Fidelity and validation\n\nChecked C-001.", encoding="utf-8"
            )
            (workspace / "agent-result.json").write_text(
                json.dumps(generated_result()), encoding="utf-8"
            )

            report = visualization_validation.validate(
                workspace=workspace, expectations={"claim_ids": ["C-001"]}
            )

            self.assertTrue(report.valid, report.failure_message())

    def test_rejects_unknown_claims_unlisted_files_and_traversal(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = workspace / "visualization"
            directory.mkdir()
            (directory / "index.html").write_text("<title>Test</title>", encoding="utf-8")
            (directory / "verification.md").write_text("# Checks", encoding="utf-8")
            (directory / "extra.js").write_text("", encoding="utf-8")
            result = generated_result([
                "visualization/index.html",
                "visualization/verification.md",
                "visualization/../escape.js",
            ])
            result["claim_refs"] = ["C-999"]
            (workspace / "agent-result.json").write_text(json.dumps(result), encoding="utf-8")

            report = visualization_validation.validate(
                workspace=workspace, expectations={"claim_ids": ["C-001"]}
            )

            self.assertFalse(report.valid)
            self.assertIn("E_CLAIM_REF", report.failure_message())
            self.assertIn("E_FILE_PATH", report.failure_message())
            self.assertIn("unlisted visualization file", report.failure_message())

    def test_fidelity_review_covers_every_declared_claim(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = {
                "fidelity": "well_supported",
                "exposition_quality": "complete",
                "interaction_quality": "works",
                "summary": "The construction matches the claim.",
                "claim_reviews": [
                    {
                        "claim_id": "C-001",
                        "assessment": "faithful",
                        "explanation": "Every displayed operation matches the proof.",
                    }
                ],
                "mathematical_findings": [],
                "exposition_findings": [],
                "interaction_findings": [],
                "blocking_gaps": [],
                "warnings": [],
            }
            (workspace / "agent-result.json").write_text(json.dumps(result), encoding="utf-8")
            (workspace / "fidelity-critique.md").write_text(
                "# Fidelity review\n\nC-001 is represented faithfully.", encoding="utf-8"
            )

            valid = review_validation.validate(
                workspace=workspace, expectations={"claim_ids": ["C-001"]}
            )
            self.assertTrue(valid.valid, valid.failure_message())

            invalid = review_validation.validate(
                workspace=workspace,
                expectations={"claim_ids": ["C-001", "C-002"]},
            )
            self.assertFalse(invalid.valid)
            self.assertIn("missing review for C-002", invalid.failure_message())

    def test_major_exposition_failure_requires_a_blocking_gap(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = {
                "fidelity": "well_supported",
                "exposition_quality": "not_self_contained",
                "interaction_quality": "works",
                "summary": "The examples work but the theorem is missing.",
                "claim_reviews": [],
                "mathematical_findings": [],
                "exposition_findings": ["The exact result is not stated."],
                "interaction_findings": [],
                "blocking_gaps": [],
                "warnings": [],
            }
            (workspace / "agent-result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            (workspace / "fidelity-critique.md").write_text(
                "# Fidelity review\n\nThe theorem is not stated.",
                encoding="utf-8",
            )

            report = review_validation.validate(
                workspace=workspace, expectations={"claim_ids": []}
            )

            self.assertFalse(report.valid)
            self.assertIn("E_EXPOSITION_GAPS", report.failure_message())


class VisualizationDiscoveryTests(unittest.TestCase):
    def test_discovers_reviewed_package_and_scopes_resources(self):
        with TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempt-001"
            package = attempt / "visualizations" / "visualization-001"
            package.mkdir(parents=True)
            (package / "index.html").write_text("<title>Test</title>", encoding="utf-8")
            (package / "app.js").write_text("", encoding="utf-8")
            (package / "visualization.json").write_text(
                json.dumps({
                    "title": "Construction",
                    "summary": "A running example.",
                    "status": "complete",
                    "entry_point": "index.html",
                    "claim_refs": ["C-001"],
                    "concepts": [],
                    "limitations": [],
                    "warnings": [],
                }),
                encoding="utf-8",
            )
            (package / "fidelity-review.json").write_text(
                json.dumps({
                    "fidelity": "minor_gaps",
                    "exposition_quality": "complete",
                    "interaction_quality": "works",
                    "summary": "One limitation needs emphasis.",
                    "blocking_gaps": [],
                }),
                encoding="utf-8",
            )

            records = visualizations.discover(attempt)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["fidelity"], "minor_gaps")
            self.assertEqual(records[0]["expositionQuality"], "complete")
            self.assertEqual(
                visualizations.resolve_file(package, "app.js"),
                (package / "app.js").resolve(),
            )
            with self.assertRaises(ValueError):
                visualizations.resolve_file(package, "../visualization.json")


if __name__ == "__main__":
    unittest.main()
