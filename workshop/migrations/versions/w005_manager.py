"""Persist bounded manager wakeups and a privacy-filtered, replayable organization history."""

from alembic import op

revision = "w005"
down_revision = "w004"

# Every history record has a deliberately narrow projection. No private bodies, tokens,
# raw candidate artifacts, review opinions or NF knowledge cuts enter this table.
SOURCES = {
    "aw_employment_versions": (
        "employee_id",
        "employee_id",
        "employment_revision",
        ("employee_id", "role_id", "status", "employment_revision", "org_revision"),
    ),
    "aw_delegations": (
        "operation_id",
        "owner_id",
        "owner_revision",
        ("operation_id", "state", "owner_id", "last_error"),
    ),
    "aw_remote_receipts": (
        "operation_id",
        None,
        None,
        ("operation_id", "task_id", "generation", "task_state", "accounting_state", "world_ref"),
    ),
    "aw_budget_holds": (
        "operation_id",
        None,
        None,
        ("operation_id", "amount", "status", "settled_amount"),
    ),
    "aw_reviews": (
        "review_id",
        "reviewer_id",
        "reviewer_revision",
        ("review_id", "mode", "round_no", "state", "deadline"),
    ),
    "aw_deliveries": (
        "message_id",
        "recipient",
        "recipient_revision",
        ("message_id", "seq", "acknowledged_at"),
    ),
    "aw_resume_requests": ("request_id", None, None, ("request_id", "state", "attempts")),
}


def upgrade():
    op.execute("""
        ALTER TABLE aw_remote_receipts ADD COLUMN world_ref jsonb;
        CREATE TABLE aw_resume_requests (
            org_id text REFERENCES aw_organizations, request_id uuid PRIMARY KEY, installation_id text NOT NULL REFERENCES aw_installations,
            employee_id text NOT NULL, employment_revision bigint NOT NULL, binding_digest char(64) NOT NULL REFERENCES aw_bindings,
            operation_id text NOT NULL, state text NOT NULL, attempts integer NOT NULL DEFAULT 0,
            eligible_at timestamptz NOT NULL DEFAULT now(), result jsonb,
            UNIQUE(org_id,employee_id,employment_revision,operation_id)
        );
        CREATE INDEX aw_resume_due ON aw_resume_requests(org_id,installation_id,eligible_at);
        CREATE TABLE aw_source_cuts (
            org_id text PRIMARY KEY REFERENCES aw_organizations, cut bigint NOT NULL,
            cursor_key uuid NOT NULL DEFAULT gen_random_uuid()
        );
        CREATE TABLE aw_change_log (
            org_id text NOT NULL REFERENCES aw_organizations, seq bigint NOT NULL,
            source text NOT NULL, ref_id text NOT NULL, employee_id text, employment_revision bigint,
            data jsonb NOT NULL, PRIMARY KEY(org_id,seq)
        );
        CREATE INDEX aw_changes_viewer ON aw_change_log(org_id,employee_id,employment_revision,seq);
        CREATE FUNCTION aw_append_projection(o text,s text,r text,e text,v bigint,d jsonb)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE n bigint;
        BEGIN
          INSERT INTO aw_source_cuts(org_id,cut) VALUES(o,1)
            ON CONFLICT(org_id) DO UPDATE SET cut=aw_source_cuts.cut+1 RETURNING cut INTO n;
          INSERT INTO aw_change_log VALUES(o,n,s,r,e,v,d);
        END $$;
    """)
    for table, (key, emp, version, fields) in SOURCES.items():

        def args(prefix):
            projection = (
                "jsonb_build_object("
                + ",".join(f"'{field}',{prefix}{field}" for field in fields)
                + ")"
            )
            return ",".join(
                (
                    prefix + "org_id",
                    "'" + table + "'",
                    prefix + key + "::text",
                    prefix + emp if emp else "NULL",
                    prefix + version if version else "NULL",
                    projection,
                )
            )

        op.execute(
            f"""CREATE FUNCTION {table}_history() RETURNS trigger LANGUAGE plpgsql AS $$
              BEGIN PERFORM aw_append_projection({args("NEW.")}); RETURN NULL; END $$;
            CREATE TRIGGER aw_history AFTER INSERT OR UPDATE ON {table}
              FOR EACH ROW EXECUTE FUNCTION {table}_history();
            SELECT aw_append_projection({args("x.")}) FROM {table} x ORDER BY x.org_id,x.{key}"""
            + (",x.employment_revision" if table == "aw_employment_versions" else "")
        )


def downgrade():
    raise RuntimeError("Restore the paired backup to retain source cuts and resume receipts")
