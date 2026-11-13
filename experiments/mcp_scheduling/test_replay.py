"""Scheduler mechanism tests; these fixtures have no MCP, DB or Provider side effects."""

import random
import unittest

from experiments.mcp_scheduling.replay import (
    ARMS, ELIGIBLE, QUEUE_LIMIT, Arm, Item, Request, Router, Waiting, replay, workload,
)


def item(seq, work, kind="read", due=0):
    return Item(Request(seq, due, work, kind, 2, 0), due, seq)


class SchedulingTests(unittest.TestCase):
    def test_admission_bound_and_lease_reservation(self):
        for arm in ARMS.values():
            q = Waiting(arm)
            accepted = [q.push(item(i, "alpha")) for i in range(2 * QUEUE_LIMIT)]
            self.assertEqual(sum(accepted), QUEUE_LIMIT // 2 if arm.reserve else QUEUE_LIMIT)
            self.assertEqual(q.push(item(100, "alpha", "lease")), arm.reserve)
            self.assertLessEqual(q.count, QUEUE_LIMIT)

    def test_routing_never_leaves_trusted_set(self):
        rng = random.Random(99)
        for policy in ("rr", "least", "p2"):
            router = Router(policy, 42)
            for _ in range(50):
                loads = [rng.randrange(9) for _ in range(4)]
                for work, eligible in ELIGIBLE.items():
                    selected = router.select(work, loads)
                    self.assertIn(selected, eligible)
                    if policy == "least":
                        self.assertEqual(loads[selected], min(loads[i] for i in eligible))

    def test_fifo_oldest_and_drr_fair_competition(self):
        for arm in (Arm(), Arm(queue="wfq"), Arm(queue="drr")):
            q = Waiting(arm)
            for seq in range(12):
                q.push(item(seq, "alpha", "context"))
            q.push(item(20, "beta", "lease"))
            order = [q.pop(t).request.seq for t in range(q.count)]
            self.assertEqual(sorted(order), list(range(12)) + [20])
            if arm.queue == "fifo":
                self.assertEqual(order[-1], 20)
            else:
                self.assertLess(order.index(20), 3)

    def test_age_cap_overrides_flow_priority(self):
        q = Waiting(Arm(queue="drr", ageing=True))
        q.push(item(0, "alpha", "context", due=0))
        q.push(item(1, "beta", "lease", due=19))
        self.assertEqual(q.pop(19).request.seq, 0)

    def test_detached_wait_leaves_worker_idle_and_wakes_once(self):
        reqs = [Request(0, 0, "delta", "model", 2, 50), Request(1, 7, "delta", "read", 2, 0)]
        summary, rows = replay(reqs, Arm(detach=True), 42)
        self.assertEqual(rows[0].occupied, [(0, 2), (52, 54)])
        self.assertEqual((rows[1].first, rows[1].end), (7, 11))
        self.assertEqual(summary["slot_ticks"], 8)
        self.assertEqual(summary["peaks"]["external_wait"], 1)
        _, held = replay(reqs, Arm(), 42)
        self.assertEqual(held[1].first, 54)

    def test_empty_input_no_resources_or_events(self):
        for arm in ARMS.values():
            summary, rows = replay([], arm, 15)
            self.assertEqual(rows, [])
            self.assertEqual((summary["slot_ticks"], summary["elapsed"], summary["drain_ticks"]),
                             (0, 0, 0))
            self.assertIsNone(summary["p95"])

    def test_replay_is_reproducible_finite_and_accounted(self):
        reqs = workload("burst", 42)
        for name, arm in ARMS.items():
            with self.subTest(arm=name):
                summary, rows = replay(reqs, arm, 82)
                self.assertEqual((summary, rows), replay(reqs, arm, 82))
                self.assertEqual(sum(summary["states"].values()), len(reqs))
                self.assertTrue(set(summary["states"]) <= {"done", "rejected", "wake_rejected"})
                self.assertLessEqual(summary["peaks"]["replica_queue"], QUEUE_LIMIT)
                self.assertLessEqual(summary["peaks"]["busy"], 4)
                self.assertTrue(all(r.end >= r.due for r in rows))
                used = [(event, sign) for r in rows for a, b in r.occupied
                        for event, sign in ((a, 1), (b, -1))]
                active = 0
                for _, sign in sorted(used):
                    active += sign
                    self.assertTrue(0 <= active <= 4)
                self.assertEqual(active, 0)
                for worker in range(4):
                    occupied = sorted(interval for r in rows if r.replica == worker
                                      for interval in r.occupied)
                    self.assertTrue(all(b <= c for (_, b), (c, _) in zip(occupied, occupied[1:])))


if __name__ == "__main__":
    unittest.main()
