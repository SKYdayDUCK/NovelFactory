"""Operator-only, minute-bucketed infrastructure observations."""

from alembic import op

revision = "0012_p55_diagnostics"
down_revision = "0011_p55_statistics"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_service_diagnostics (
            bucket timestamptz NOT NULL,
            component text NOT NULL CHECK (component IN ('relay','model','world','compute','recovery','log','statistics')),
            code text NOT NULL CHECK(code IN ('REDIS_UNAVAILABLE','PUBLISH_FAILED','REDIS_COMMAND_FAILED',
                'DATABASE_UNAVAILABLE','DATABASE_TIMEOUT','DATABASE_ERROR','PENDING_RECLAIMED')),
            first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
            samples bigint NOT NULL CHECK(samples>0), recovered_at timestamptz,
            PRIMARY KEY(component,code,bucket)
        );
        CREATE INDEX nf_ops_active ON nf_service_diagnostics(component,code)
            WHERE recovered_at IS NULL AND code <> 'PENDING_RECLAIMED';
        CREATE INDEX nf_ops_recent ON nf_service_diagnostics(last_seen DESC)
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_service_diagnostics IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM nf_service_diagnostics)
            THEN RAISE EXCEPTION 'Archive service incident evidence before downgrade'; END IF; END $$;
        DROP TABLE nf_service_diagnostics
    """)
