import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_worked_example import validate_evidence

SOURCE, TEMPLATES = "a" * 40, "b" * 40


def evidence():
    checks = []
    for step, slot, revision in (
            ("initial-active", "aks01", "c" * 40),
            ("standby-updated-active-unchanged", "aks01", "c" * 40),
            ("traffic-cutover", "aks02", "d" * 40),
            ("traffic-rollback", "aks01", "c" * 40)):
        checks.append({"step": step, "endpoint": "127.0.0.1:23456", "expected_slot": slot,
                       "responses": [{"slot": slot, "source_commit": revision, "tls_verified": True}
                                     for _ in range(2)]})
    return {"result": "passed", "cleanup_errors": [], "public_source_commit": SOURCE,
            "tiers": [{"tier": n, "result": "passed"} for n in (1, 2, 3)],
            "traffic_checks": checks,
            "tier_execution": [{"tier": tier, "slot": slot, "result": "passed",
                                "template": {"repository_commit": TEMPLATES, "working_tree_modified": False}}
                               for tier in ("platform", "application") for slot in ("aks01", "aks02")]}


class WorkedEvidenceTests(unittest.TestCase):
    def test_complete_direct_evidence_accepted(self):
        validate_evidence(evidence(), "direct", SOURCE, TEMPLATES)

    def test_argocd_does_not_require_imperative_application_execution(self):
        report = evidence()
        report["tier_execution"] = report["tier_execution"][:2]
        validate_evidence(report, "argocd", SOURCE, TEMPLATES)
        with self.assertRaises(ValueError):
            validate_evidence(report, "direct", SOURCE, TEMPLATES)

    def test_cleanup_failure_or_missing_tier_rejects_overall_success(self):
        for change in (lambda x: x.update(cleanup_errors=["cluster remains"]),
                       lambda x: x.pop("cleanup_errors"),
                       lambda x: x["tiers"].pop(1),
                       lambda x: x["tiers"][1].update(result="failed")):
            report = evidence()
            change(report)
            with self.assertRaises(ValueError):
                validate_evidence(report, "direct", SOURCE, TEMPLATES)

    def test_changing_endpoint_is_not_a_traffic_cutover(self):
        report = evidence()
        report["traffic_checks"][2]["endpoint"] = "127.0.0.1:23457"
        with self.assertRaises(ValueError):
            validate_evidence(report, "direct", SOURCE, TEMPLATES)

    def test_unchanged_release_or_wrong_returned_slot_is_rejected(self):
        for change in (
                lambda x: x["traffic_checks"][2]["responses"][0].update(slot="aks01"),
                lambda x: [row.update(source_commit="c" * 40)
                           for row in x["traffic_checks"][2]["responses"]],
                lambda x: x["traffic_checks"][3]["responses"][0].update(tls_verified=False)):
            report = evidence()
            change(report)
            with self.assertRaises(ValueError):
                validate_evidence(report, "direct", SOURCE, TEMPLATES)

    def test_unreviewed_template_or_different_source_cannot_reuse_proof(self):
        for change in (
                lambda x: x.update(public_source_commit="e" * 40),
                lambda x: x["tier_execution"][0]["template"].update(working_tree_modified=True),
                lambda x: x["tier_execution"][0]["template"].update(repository_commit="f" * 40)):
            report = evidence()
            change(report)
            with self.assertRaises(ValueError):
                validate_evidence(report, "direct", SOURCE, TEMPLATES)


if __name__ == "__main__":
    unittest.main()
