"""Versioned organizations, employment, routing graphs and scoped receipts."""

from alembic import op

revision = "w001"
down_revision = "w000"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE aw_organizations (
            org_id text PRIMARY KEY, active_revision bigint NOT NULL CHECK (active_revision > 0),
            preset text NOT NULL, preset_sha text NOT NULL, scope_ref text NOT NULL
        );
        CREATE TABLE aw_org_revisions (
            org_id text NOT NULL REFERENCES aw_organizations, revision bigint NOT NULL,
            preset text NOT NULL, preset_sha text NOT NULL, changed_by text NOT NULL,
            change_kind text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id,revision)
        );
        CREATE TABLE aw_employees (
            org_id text NOT NULL REFERENCES aw_organizations, employee_id text NOT NULL,
            employment_revision bigint NOT NULL, role_id text NOT NULL, team_id text NOT NULL,
            supervisor_id text, scope_ref text NOT NULL, status text NOT NULL
                CHECK (status IN ('ACTIVE','SUSPENDED','ENDED')),
            PRIMARY KEY (org_id,employee_id),
            FOREIGN KEY (org_id,supervisor_id) REFERENCES aw_employees (org_id,employee_id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX aw_reports_to ON aw_employees (org_id,supervisor_id) WHERE status='ACTIVE';
        CREATE TABLE aw_employment_versions (
            org_id text NOT NULL, employee_id text NOT NULL, employment_revision bigint NOT NULL,
            org_revision bigint NOT NULL, role_id text NOT NULL, team_id text NOT NULL,
            supervisor_id text, scope_ref text NOT NULL, status text NOT NULL,
            PRIMARY KEY (org_id,employee_id,employment_revision),
            FOREIGN KEY (org_id,employee_id) REFERENCES aw_employees,
            FOREIGN KEY (org_id,org_revision) REFERENCES aw_org_revisions
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE aw_edges (
            org_id text NOT NULL, graph_revision bigint NOT NULL, src text NOT NULL,
            dst text NOT NULL, kind text NOT NULL, scope_ref text NOT NULL,
            PRIMARY KEY (org_id,graph_revision,src,dst,kind,scope_ref),
            FOREIGN KEY (org_id,graph_revision) REFERENCES aw_org_revisions,
            FOREIGN KEY (org_id,src) REFERENCES aw_employees,
            FOREIGN KEY (org_id,dst) REFERENCES aw_employees
        );
        CREATE INDEX aw_edges_recipient ON aw_edges (org_id,graph_revision,dst);
        CREATE VIEW aw_roster_views AS
          SELECT v.org_id, o.active_revision AS revision, v.employee_id AS audience,
                 e.employee_id,e.role_id,e.team_id,e.supervisor_id,e.employment_revision,e.status
          FROM aw_employees v JOIN aw_organizations o USING (org_id)
          JOIN aw_employees e ON e.org_id=v.org_id
          WHERE v.status='ACTIVE' AND (v.role_id IN ('workshop_manager','author')
            OR e.employee_id=v.employee_id OR (e.status='ACTIVE' AND EXISTS (
              SELECT 1 FROM aw_edges r WHERE r.org_id=v.org_id AND r.graph_revision=o.active_revision
                AND ((r.src=v.employee_id AND r.dst=e.employee_id)
                  OR (r.dst=v.employee_id AND r.src=e.employee_id)))));
        CREATE TABLE aw_org_operations (
            org_id text NOT NULL REFERENCES aw_organizations, employee_id text NOT NULL,
            employment_revision bigint NOT NULL, operation_id text NOT NULL,
            request_sha text NOT NULL, receipt jsonb NOT NULL,
            PRIMARY KEY (org_id,employee_id,employment_revision,operation_id)
        );
    """)


def downgrade():
    raise RuntimeError("Restore a paired backup to retain employment and authorization history")
