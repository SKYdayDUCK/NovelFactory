"""Append authenticated Skill freezes and remote receipt identifiers to the existing log.

Historical cuts remain unchanged. No new ledger or historical payload backfill.
"""
from alembic import op

revision = "w011"
down_revision = "w010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION aw_remote_receipts_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM aw_append_projection(NEW.org_id,'aw_remote_receipts',NEW.operation_id::text,NULL,NULL,
            jsonb_build_object('operation_id',NEW.operation_id,'receipt_ref',NEW.receipt_ref,'task_id',NEW.task_id,
             'generation',NEW.generation,'task_state',NEW.task_state,'accounting_state',NEW.accounting_state,
             'world_ref',NEW.world_ref));
          RETURN NULL;
        END $$;
        CREATE FUNCTION aw_harness_freeze_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM aw_append_projection(NEW.org_id,'harness.skill.frozen',NEW.binding_id::text,NEW.employee_id,
            NEW.employment_revision, jsonb_build_object('stage','S4','installation_id',d.installation_id,
              'operation_id',r.operation_id,'receipt_ref',r.receipt_ref,'task_id',r.task_id,
              'receipt_generation',r.generation,'world_ref',r.world_ref,'binding_id',NEW.binding_id,
              'skill_epoch',NEW.epoch,'skill_content_sha256',NEW.content_sha,'employment_revision',NEW.employment_revision))
            FROM aw_remote_receipts r JOIN aw_delegations d USING(org_id,operation_id)
            WHERE r.org_id=NEW.org_id AND r.task_id=NEW.task_ref;
          RETURN NULL;
        END $$;
        CREATE TRIGGER aw_harness_history AFTER INSERT ON aw_skill_snapshots
          FOR EACH ROW EXECUTE FUNCTION aw_harness_freeze_history();
    """)


def downgrade():
    raise RuntimeError("Restore the paired backup to retain authenticated history and its source cuts")
