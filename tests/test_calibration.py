import unittest
from astrbot_plugin_memos_memory.calibration import (
    BASELINE, POLICY_MANIFEST, calibrate, score_cases, split_cases,
)


class CalibrationTests(unittest.TestCase):
    def case(self, groups, **extra):
        return {"groups": groups, "features": dict.fromkeys("abcde", 1.0),
                "expected": "accepted", "label_kind": "weak", "gap": .2, **extra}

    def test_transitive_groups_never_leak(self):
        cases = [self.case(["a", "b"]), self.case(["c", "d"]), self.case(["b", "c"])]
        split = split_cases(cases)
        self.assertEqual(sorted(map(len, split.values())), [0, 0, 3])

    def test_no_predictions_is_not_perfect_precision(self):
        self.assertIsNone(score_cases([], BASELINE)["precision"])

    def test_weak_labels_cannot_activate(self):
        report = calibrate([self.case([str(i)]) for i in range(200)])
        self.assertFalse(report["activate"])
        self.assertFalse(report["eligible_for_review"])

    def test_causal_stays_deferred(self):
        case = self.case(["a"], relation="causes", features=dict.fromkeys("abcde", .9))
        self.assertEqual(score_cases([case], BASELINE)["tp"], 0)

    def test_missing_groups_rejected(self):
        with self.assertRaises(ValueError):
            split_cases([self.case([])])

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError):
            score_cases([self.case(["a"], features={"b": float("nan")})], BASELINE)

    def test_conflicting_partition_rejected(self):
        with self.assertRaises(ValueError):
            split_cases([self.case(["a"], partition="train"),
                         self.case(["a"], partition="test")])

    def test_declared_partition_preserved(self):
        result = split_cases([self.case(["a"], partition="test")])
        self.assertEqual(len(result["test"]), 1)

    def test_no_labels_preserves_baseline_proposal(self):
        result = calibrate([self.case(["a"], expected="unknown")])
        self.assertFalse(result["proposal_supported"])
        self.assertEqual(result["proposal"], BASELINE)

    def test_runtime_baseline_parity(self):
        import random
        from unittest.mock import patch
        from astrbot_plugin_memos_memory.thread_arbiter import ThreadArbiter
        from astrbot_plugin_memos_memory.thread_evidence import EvidenceResult
        rng = random.Random(9)
        arbiter = ThreadArbiter()
        for i in range(200):
            result = EvidenceResult()
            for key in "abcde":
                setattr(result, key, rng.random())
            result.hard_reject = i % 7 == 0
            result.details["date_conflict"] = i % 5 == 0
            gap = None if i % 3 == 0 else rng.random()
            relation = "causes" if i % 4 == 0 else "parallel"
            with patch.object(arbiter._evidence, "compute", return_value=result), patch.object(arbiter, "_infer_relation", return_value=(relation, "none")):
                edge = arbiter.decide({"episode_id": "a"}, {"episode_id": "b"}, candidate_gap=gap)
            case = self.case(["a"], gap=gap, relation=relation,
                             features={k: getattr(result, k) for k in "abcde"},
                             hard_reject=result.hard_reject,
                             date_conflict=result.details["date_conflict"])
            self.assertEqual(score_cases([case], BASELINE)["tp"], int(edge["status"] == "accepted"))

    def test_policy_manifest_tracks_complete_pipeline(self):
        self.assertEqual(POLICY_MANIFEST["parent"], "6.0.0-test8ultra4")
        for key in ("thread_link", "prospective", "retrieval", "injection", "consistency"):
            self.assertIn(key, POLICY_MANIFEST)

    def test_relation_threshold_is_independent(self):
        policy = {**BASELINE, "accept": .5,
                  "thresholds_by_relation": {"continues": .95}}
        case = self.case(["relation"], relation="continues",
                         features=dict.fromkeys("abcde", .9), gap=.3)
        result = score_cases([case], policy)
        self.assertEqual(result["decisions"]["deferred"], 1)

    def test_policy_selection_requires_validation_labels(self):
        cases = [
            self.case(["train"], partition="train"),
            self.case(["validation"], partition="validation"),
            self.case(["test"], partition="test"),
        ]
        result = calibrate(cases)
        self.assertTrue(result["proposal_supported"])
        self.assertEqual(result["selection_protocol"], "validation_rank_after_train_enumeration")
