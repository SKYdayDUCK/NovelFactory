"""Frozen skill provenance, review evidence and independent reviewer assignments."""

from alembic import op

revision = "w004"
down_revision = "w003"


def upgrade():
    op.execute("""
        ALTER TABLE aw_delegation_slots ADD COLUMN review_required boolean NOT NULL DEFAULT false;
        CREATE TABLE aw_skill_sources (
          org_id text REFERENCES aw_organizations, skill_id text, source_revision text, content_sha char(64),
          namespace text NOT NULL CHECK (namespace IN ('creative','control')),
          mode text NOT NULL, PRIMARY KEY(org_id,skill_id,source_revision)
        );
        CREATE TABLE aw_skill_bindings (
          org_id text NOT NULL, binding_id uuid NOT NULL, skill_id text NOT NULL, source_revision text NOT NULL,
          content_sha char(64) NOT NULL, scope_ref text NOT NULL, policy text NOT NULL, target text,
          owner text NOT NULL, owner_revision bigint NOT NULL, mode text NOT NULL,
          expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false, epoch bigint NOT NULL DEFAULT 1,
          PRIMARY KEY(org_id,binding_id), FOREIGN KEY(org_id,skill_id,source_revision) REFERENCES aw_skill_sources,
          FOREIGN KEY(org_id,owner,owner_revision) REFERENCES aw_employment_versions
        );
        CREATE TABLE aw_skill_snapshots (
          org_id text NOT NULL, employee_id text NOT NULL, employment_revision bigint NOT NULL, task_ref uuid NOT NULL,
          skill_id text NOT NULL, binding_id uuid NOT NULL, epoch bigint NOT NULL, content_sha char(64) NOT NULL,
          PRIMARY KEY(org_id,employee_id,employment_revision,task_ref,skill_id),
          FOREIGN KEY(org_id,binding_id) REFERENCES aw_skill_bindings
        );
        CREATE TABLE aw_review_identities (
          org_id text NOT NULL, employee_id text NOT NULL, subject_group text NOT NULL,
          PRIMARY KEY(org_id,employee_id), FOREIGN KEY(org_id,employee_id) REFERENCES aw_employees
        );
        CREATE TABLE aw_review_artifacts (
          org_id text NOT NULL, artifact_ref text NOT NULL, artifact_revision text NOT NULL, content_sha char(64) NOT NULL,
          scope_ref text NOT NULL, source_receipt text NOT NULL, producer_groups text[] NOT NULL,
          producer_ids text[] NOT NULL, evidence_refs text[] NOT NULL, rule_results jsonb NOT NULL,
          PRIMARY KEY(org_id,artifact_ref,artifact_revision), UNIQUE(org_id,source_receipt)
        );
        CREATE TABLE aw_review_slots (
          org_id text NOT NULL, slot_id text NOT NULL, artifact_ref text NOT NULL, artifact_revision text NOT NULL, mode text NOT NULL,
          PRIMARY KEY(org_id,slot_id), FOREIGN KEY(org_id,slot_id) REFERENCES aw_delegation_slots,
          FOREIGN KEY(org_id,artifact_ref,artifact_revision) REFERENCES aw_review_artifacts
        );
        CREATE TABLE aw_reviews (
          org_id text NOT NULL, review_id uuid NOT NULL, artifact_ref text NOT NULL, artifact_revision text NOT NULL,
          content_sha char(64) NOT NULL, mode text NOT NULL, round_no integer NOT NULL CHECK (round_no BETWEEN 1 AND 2),
          policy_revision text NOT NULL, requested_by text NOT NULL, risk_reason text NOT NULL,
          reviewer_id text, reviewer_revision bigint, execution text NOT NULL, state text NOT NULL, evidence_ref text,
          decision_reason text, deadline timestamptz NOT NULL, operation_id uuid, budget_amount numeric(20,6) NOT NULL,
          token_limit bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(org_id,review_id), UNIQUE(org_id,content_sha,mode,policy_revision,round_no),
          FOREIGN KEY(org_id,artifact_ref,artifact_revision) REFERENCES aw_review_artifacts,
          FOREIGN KEY(org_id,reviewer_id,reviewer_revision) REFERENCES aw_employment_versions,
          FOREIGN KEY(org_id,operation_id) REFERENCES aw_delegations, UNIQUE(org_id,operation_id)
        );
        CREATE INDEX aw_open_reviews ON aw_reviews(org_id,reviewer_id,deadline) WHERE state='PENDING';
        CREATE TABLE aw_review_evidence (
          org_id text NOT NULL, review_id uuid NOT NULL, seq integer NOT NULL, authority text NOT NULL, reason text NOT NULL,
          evidence_ref text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(org_id,review_id,seq), FOREIGN KEY(org_id,review_id) REFERENCES aw_reviews
        );
    """)


def downgrade():
    raise RuntimeError("Restore an approved paired backup to preserve signed review decisions")
