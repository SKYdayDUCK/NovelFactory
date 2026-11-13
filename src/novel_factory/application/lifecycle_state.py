"""Content-free change tokens for a sleeping, authorized project supervisor."""

from sqlalchemy import text


def project_change_token(kernel, principal, work, branch):
    """Check live authorization, then hash durable scheduling inputs in one snapshot.

    Logs, observation timestamps and artifact UUID churn are deliberately absent:
    a sleeping manager's own diagnostics must not wake it into a retry loop.
    Deadline/lease predicates change when time passes even without NOTIFY.
    """
    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        grant = kernel.authorize(repo, principal, work, "ADMIN", branch)
        token = repo.conn.scalar(text("""
            SELECT md5(concat(
              (SELECT policy_version FROM nf_works WHERE work_id=:work),
              (SELECT jsonb_agg(jsonb_build_array(task_id,generation,state,current_result_id,
                        lease_expiry IS NOT NULL AND lease_expiry<=statement_timestamp(),
                        deadline<=statement_timestamp()) ORDER BY task_id)::text
               FROM nf_tasks WHERE work_id=:work AND branch_id=:branch),
              (SELECT jsonb_agg(jsonb_build_array(s.selection_kind,s.artifact_ref->>'sha256')
                        ORDER BY s.selection_kind)::text FROM nf_selections s JOIN nf_artifacts a
                        ON a.artifact_revision_id=(s.artifact_ref->>'artifact_revision_id')::uuid
               WHERE s.work_id=:work AND (a.payload#>>'{world_ref,branch_id}'=CAST(:branch AS text)
                                          OR a.payload->>'branch_id'=CAST(:branch AS text))),
              (SELECT jsonb_agg(jsonb_build_array(c.attempt_id,c.state,c.actual_amount,c.actual_tokens,c.provider_receipt)
                        ORDER BY c.attempt_id)::text FROM nf_call_attempts c JOIN nf_tasks t USING(task_id)
               WHERE t.work_id=:work AND t.branch_id=:branch),
              (SELECT jsonb_agg(jsonb_build_array(b.budget_id,b.limit_amount,b.reserved_amount,b.spent_amount,l.total_tokens)
                        ORDER BY b.budget_id)::text FROM nf_budgets b
                        LEFT JOIN nf_budget_token_limits l USING(budget_id) WHERE b.work_id=:work),
              (SELECT jsonb_agg(jsonb_build_array(d.task_id,d.generation,d.retry_not_before<=statement_timestamp())
                        ORDER BY d.task_id)::text FROM nf_quota_retry_deadlines d JOIN nf_tasks t USING(task_id)
               WHERE t.work_id=:work AND t.branch_id=:branch),
              CAST(:epoch AS text)
            ))
        """),
            {"work": work, "branch": branch, "epoch": grant["epoch"]})
        return token
