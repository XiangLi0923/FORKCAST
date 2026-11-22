import unittest
from dataclasses import replace
from fractions import Fraction

from forkcast.decision_ranking import RankKey, rank_options, score_factors


class DecisionRankingTests(unittest.TestCase):
    def test_d05_approved_near_optimal_and_diversity_vectors(self):
        vector = tuple(RankKey(bundle, venue, family, score, queue, "MEDIUM") for
                       bundle, score, queue, venue, family in (
            ("A", 800, 5, "va", "f1"), ("B", 750, 4, "vb", "f2"),
            ("C", 749, 1, "vc", "f3"), ("D", 740, 6, "vb", "f2"),
            ("E", 730, 7, "va", "f4"),
        ))
        ranking = rank_options(vector, 4)
        self.assertEqual(ranking.primary.bundle_id, "B")
        self.assertEqual(tuple(row.bundle_id for row in ranking.alternatives), ("A", "C", "E"))
        self.assertEqual(ranking.queue_comparator.bundle_id, "A")
        self.assertEqual(rank_options(tuple(reversed(vector)), 4), ranking)
        below = (vector[0], replace(vector[1], score=749), *vector[2:])
        ranking = rank_options(below, 4)
        self.assertEqual(ranking.primary.bundle_id, "A")
        self.assertEqual(tuple(row.bundle_id for row in ranking.alternatives), ("B", "C", "E"))

    def test_r04_equal_scores_use_queue_confidence_and_case_sensitive_id(self):
        vector = (
            RankKey("A", "va", "f1", 800, 5, "LOW"),
            RankKey("B", "vb", "f1", 800, 5, "MEDIUM"),
            RankKey("C", "vc", "f1", 800, 5, "MEDIUM", True),
        )
        ranking = rank_options(vector, 1)
        self.assertEqual(ranking.primary.bundle_id, "B")
        self.assertEqual(ranking.alternatives, ())
        self.assertEqual(ranking.requested.bundle_id, "C")
        faster = (replace(vector[0], queue_upper=4), *vector[1:])
        self.assertEqual(rank_options(faster, 2).primary.bundle_id, "A")
        self.assertEqual(tuple(row.bundle_id for row in rank_options(faster, 4).alternatives), ("B", "C"))

    def test_d08_approved_exact_half_up_profiles(self):
        values = tuple(map(Fraction, ("1", "0.5", "0.25", "0.5", "0.5")))
        expected = {"DEFAULT": 638, "FAST": 600, "VALUE": 538, "EXPLORE": 600}
        for profile, score in expected.items():
            with self.subTest(profile=profile):
                checked = score_factors(profile, values)
                self.assertEqual(checked.match_score, score)
                self.assertEqual(sum(item.weight for item in checked.factors), 100)
