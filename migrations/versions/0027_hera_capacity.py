"""Opt-in transactionally enforced admission counters. No task rewrite on upgrade."""
from alembic import op

revision = "0027_hera_capacity"
down_revision = "0026_hera_progress_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE nf_durable_admission_policy
          ADD COLUMN exact_counters boolean NOT NULL DEFAULT false;
        CREATE TABLE nf_hera_capacity (
          scope text NOT NULL, subject text NOT NULL,
          used bigint NOT NULL CHECK (used >= 0), PRIMARY KEY(scope, subject)
        );

        CREATE FUNCTION nf_hera_capacity_delta(t nf_tasks, delta int,
            p nf_durable_admission_policy) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE resource text; changed int;
        BEGIN
          SELECT resource_class INTO resource FROM nf_durable_admissions WHERE task_id=t.task_id;
          resource := coalesce(resource,p.role_resources->>t.role_id,'MODEL');
          -- All writers first lock the total row, including releases. The four
          -- increments are transactional and also work on a new/empty database.
          IF delta=1 THEN
            INSERT INTO nf_hera_capacity AS c(scope,subject,used)
              VALUES ('total','*',1), ('work',t.work_id::text,1),
                     ('principal',t.principal_id,1), ('resource',resource,1)
              ON CONFLICT(scope,subject) DO UPDATE SET used=c.used+1
              WHERE NOT p.enabled OR c.used < CASE c.scope WHEN 'total' THEN p.total_limit
                WHEN 'work' THEN p.work_limit WHEN 'principal' THEN p.principal_limit
                ELSE p.resource_limit END;
            GET DIAGNOSTICS changed = ROW_COUNT;
            IF changed <> 4 THEN
              RAISE EXCEPTION USING ERRCODE='NF001', MESSAGE='Durable task capacity is full';
            END IF;
          ELSE
            UPDATE nf_hera_capacity SET used=used-1 WHERE scope='total' AND subject='*';
            IF NOT FOUND THEN RAISE EXCEPTION 'Missing global admission count'; END IF;
            UPDATE nf_hera_capacity SET used=used-1 WHERE (scope,subject) IN
              (('work',t.work_id::text),('principal',t.principal_id),('resource',resource));
            GET DIAGNOSTICS changed = ROW_COUNT;
            IF changed <> 3 THEN RAISE EXCEPTION 'Missing scoped admission count'; END IF;
            -- The total row remains the lock anchor. Expired principal/work
            -- subjects do not become a second, ever-growing history table.
            DELETE FROM nf_hera_capacity WHERE used=0 AND (scope,subject) IN
              (('work',t.work_id::text),('principal',t.principal_id),('resource',resource));
          END IF;
        END $$;

        CREATE FUNCTION nf_hera_capacity_task() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE p nf_durable_admission_policy; old_live boolean; new_live boolean;
        BEGIN
          old_live := TG_OP <> 'INSERT' AND OLD.state NOT IN ('ACCEPTED','FAILED','CANCELLED','STALE');
          new_live := TG_OP <> 'DELETE' AND NEW.state NOT IN ('ACCEPTED','FAILED','CANCELLED','STALE');
          IF old_live = new_live AND (NOT old_live OR
              (OLD.work_id,OLD.principal_id,OLD.role_id)=(NEW.work_id,NEW.principal_id,NEW.role_id)) THEN
            RETURN NULL;
          END IF;
          SELECT * INTO STRICT p FROM nf_durable_admission_policy WHERE singleton FOR SHARE;
          IF old_live THEN PERFORM nf_hera_capacity_delta(OLD,-1,p); END IF;
          IF new_live THEN PERFORM nf_hera_capacity_delta(NEW,1,p); END IF;
          RETURN NULL;
        END $$;

        CREATE TRIGGER nf_hera_capacity_task AFTER INSERT OR DELETE
          OR UPDATE OF state,work_id,principal_id,role_id ON nf_tasks
          FOR EACH ROW EXECUTE FUNCTION nf_hera_capacity_task();
        ALTER TABLE nf_tasks DISABLE TRIGGER nf_hera_capacity_task;

        -- Changes to the resource map affect legacy tasks without an admission
        -- receipt. Capacity transitions take a shared policy lock, so a fresh
        -- READ COMMITTED snapshot after acquiring its write lock is sufficient.
        CREATE FUNCTION nf_hera_capacity_config() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF current_setting('transaction_isolation') <> 'read committed' THEN
            RAISE EXCEPTION 'Capacity maintenance requires READ COMMITTED isolation';
          END IF;
          IF NEW.exact_counters THEN
            IF NOT OLD.exact_counters THEN
              ALTER TABLE nf_tasks ENABLE TRIGGER nf_hera_capacity_task;
            END IF;
            DELETE FROM nf_hera_capacity;
            INSERT INTO nf_hera_capacity(scope,subject,used)
              SELECT k.scope,k.subject,count(*)
              FROM nf_tasks t LEFT JOIN nf_durable_admissions a USING(task_id)
              CROSS JOIN LATERAL (VALUES ('total','*'),('work',t.work_id::text),
                  ('principal',t.principal_id),
                  ('resource',coalesce(a.resource_class,NEW.role_resources->>t.role_id,'MODEL'))) k(scope,subject)
              WHERE t.state NOT IN ('ACCEPTED','FAILED','CANCELLED','STALE')
              GROUP BY k.scope,k.subject;
          ELSE
            ALTER TABLE nf_tasks DISABLE TRIGGER nf_hera_capacity_task;
            DELETE FROM nf_hera_capacity;
          END IF;
          RETURN NULL;
        END $$;
        CREATE TRIGGER nf_hera_capacity_config AFTER UPDATE OF exact_counters,role_resources
          ON nf_durable_admission_policy FOR EACH ROW
          WHEN (OLD.exact_counters <> NEW.exact_counters OR
                (NEW.exact_counters AND OLD.role_resources <> NEW.role_resources))
          EXECUTE FUNCTION nf_hera_capacity_config();

        CREATE FUNCTION nf_hera_set_capacity_mode(turn_on boolean) RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM pg_advisory_xact_lock(721943001);
          LOCK TABLE nf_tasks IN SHARE ROW EXCLUSIVE MODE;
          UPDATE nf_durable_admission_policy SET exact_counters=turn_on WHERE singleton;
        END $$;
    """)


def downgrade():
    op.execute("""
        SELECT nf_hera_set_capacity_mode(false);
        DROP TRIGGER nf_hera_capacity_config ON nf_durable_admission_policy;
        DROP TRIGGER nf_hera_capacity_task ON nf_tasks;
        DROP FUNCTION nf_hera_set_capacity_mode(boolean), nf_hera_capacity_config(),
          nf_hera_capacity_task(), nf_hera_capacity_delta(nf_tasks,int,nf_durable_admission_policy);
        DROP TABLE nf_hera_capacity;
        ALTER TABLE nf_durable_admission_policy DROP COLUMN exact_counters;
    """)
