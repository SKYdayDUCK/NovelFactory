"""Work-authorized views and audited CAS edits for cross-task experiment budgets."""

from uuid import uuid4

from sqlalchemy import text

from novel_factory.application.kernel import require


class SharedBudgetService:
    def __init__(self, kernel):
        self.kernel = kernel

    def _read(self, repo, principal, work_id, branch_id, budget_id, *, write=False):
        grant = self.kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
        # A shared budget may pay for tasks on multiple branches of one work.
        require(grant["branch_ids"] == ["*"], "VIEW_DENIED", "Work-wide budget permission required")
        budget = repo.one(
            "budgets",
            {"work_id": work_id, "budget_id": budget_id},
            lock="update" if write else "share",
        )
        require(budget, "VIEW_DENIED", "Budget unavailable")
        usage = (
            repo.conn.execute(
                text("SELECT * FROM nf_shared_token_usage(:budget)"), {"budget": budget_id}
            )
            .mappings()
            .one()
        )
        limit = repo.conn.scalar(
            text("SELECT total_tokens FROM nf_budget_token_limits WHERE budget_id=:budget"),
            {"budget": budget_id},
        )
        reserved, spent = int(usage["reserved_tokens"]), int(usage["spent_tokens"])
        return {
            **{k: budget[k] for k in ("work_id", "budget_id", "currency")},
            **{k: str(budget[k]) for k in ("limit_amount", "reserved_amount", "spent_amount")},
            "total_tokens": limit,
            "reserved_tokens": reserved,
            "spent_tokens": spent,
            "available_tokens": None if limit is None else limit - reserved - spent,
            "unquantified_attempts": usage["unquantified"],
        }

    def status(self, principal, work_id, branch_id, budget_id):
        with self.kernel.db.uow(write=False) as repo:
            return self._read(repo, principal, work_id, branch_id, budget_id)

    def set_limit(
        self, principal, work_id, branch_id, budget_id, total_tokens, *, expected_total_tokens=None
    ):
        require(
            type(total_tokens) is int and 0 <= total_tokens <= 2**63 - 1,
            "INVALID_ARGUMENT",
            "Invalid token limit",
        )
        require(
            expected_total_tokens is None
            or type(expected_total_tokens) is int
            and 0 <= expected_total_tokens <= 2**63 - 1,
            "INVALID_ARGUMENT",
            "Invalid prior token limit",
        )
        with self.kernel.db.uow() as repo:
            current = self._read(repo, principal, work_id, branch_id, budget_id, write=True)
            if current["total_tokens"] == total_tokens:
                return current
            require(
                current["total_tokens"] == expected_total_tokens,
                "IDEMPOTENCY_CONFLICT",
                "Token limit changed",
            )
            require(
                not current["unquantified_attempts"],
                "CALL_RECONCILIATION_REQUIRED",
                "Legacy token usage requires reconciliation",
            )
            used = current["reserved_tokens"] + current["spent_tokens"]
            require(
                used <= total_tokens,
                "BUDGET_EXCEEDED",
                "Limit cannot discard spent or reserved Tokens",
            )
            repo.conn.execute(
                text("""INSERT INTO nf_budget_token_limits(budget_id,total_tokens) VALUES(:budget,:limit)
                ON CONFLICT (budget_id) DO UPDATE SET total_tokens=excluded.total_tokens"""),
                {"budget": budget_id, "limit": total_tokens},
            )
            repo.outbox(
                "budget.token_limit_changed",
                "budget",
                budget_id,
                {
                    "work_id": work_id,
                    "grant_id": principal.grant_id,
                    "previous": current["total_tokens"],
                    "total_tokens": total_tokens,
                },
                "budget-token-limit:" + str(uuid4()),
            )
            return {
                **current,
                "total_tokens": total_tokens,
                "available_tokens": total_tokens - used,
            }
