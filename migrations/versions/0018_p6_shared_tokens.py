"""Enforce a shared, optional Token ceiling over every task using a budget."""

from alembic import op

revision = "0018_p6_shared_tokens"
down_revision = "0017_nf_workshop_delegation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_budget_token_limits (
            budget_id uuid PRIMARY KEY REFERENCES nf_budgets,
            total_tokens bigint NOT NULL CHECK(total_tokens>=0)
        );
        CREATE INDEX nf_call_budget_token_usage ON nf_call_attempts(budget_id);
        CREATE FUNCTION nf_shared_token_usage(id uuid)
        RETURNS TABLE(reserved_tokens numeric, spent_tokens numeric, unquantified bigint)
        LANGUAGE sql STABLE AS $$
            SELECT COALESCE(SUM(a.reserved_tokens) FILTER(WHERE a.state IN ('PREPARED','SENT','UNKNOWN')),0),
                   COALESCE(SUM(a.actual_tokens) FILTER(WHERE a.state IN ('SUCCEEDED','FAILED')),0),
                   COUNT(*) FILTER(WHERE (a.state IN ('PREPARED','SENT','UNKNOWN') AND a.reserved_tokens=0)
                       OR (a.state IN ('SUCCEEDED','FAILED') AND a.actual_tokens IS NULL))
            FROM nf_call_attempts a WHERE a.budget_id=id
        $$;
        CREATE FUNCTION nf_enforce_shared_tokens() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE ceiling bigint; charged numeric;
        BEGIN
            -- The same budget row serializes reservations, settlements and limit changes.
            PERFORM 1 FROM nf_budgets WHERE budget_id=NEW.budget_id FOR UPDATE;
            SELECT total_tokens INTO ceiling FROM nf_budget_token_limits WHERE budget_id=NEW.budget_id;
            IF ceiling IS NOT NULL THEN
                SELECT reserved_tokens+spent_tokens INTO charged FROM nf_shared_token_usage(NEW.budget_id);
                IF NEW.reserved_tokens<=0 OR charged+NEW.reserved_tokens>ceiling THEN
                    RAISE EXCEPTION 'Shared token reservation exceeds its limit'
                        USING ERRCODE='23514', CONSTRAINT='ck_nf_shared_token_reservation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER nf_enforce_shared_tokens BEFORE INSERT ON nf_call_attempts
          FOR EACH ROW EXECUTE FUNCTION nf_enforce_shared_tokens();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_budget_token_limits IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM nf_budget_token_limits) THEN
            RAISE EXCEPTION 'Restore paired data or retain active shared Token limits'; END IF; END $$;
        DROP TRIGGER nf_enforce_shared_tokens ON nf_call_attempts;
        DROP FUNCTION nf_enforce_shared_tokens(), nf_shared_token_usage(uuid);
        DROP INDEX nf_call_budget_token_usage;
        DROP TABLE nf_budget_token_limits;
    """)
