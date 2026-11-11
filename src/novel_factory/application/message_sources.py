"""Issue immutable message text revisions through the existing artifact and receipt stores."""
from hashlib import sha256

from sqlalchemy import text

from novel_factory.application.kernel import require
from novel_factory.contracts.registry import validate_contract


class MessageSourceService:
    def __init__(self, kernel):
        self.kernel = kernel

    def save_version(self, principal, world_ref, body, idempotency_key, *, prior_ref=None):
        """Create a source, or an alternative revision of a known message.

        Referencing a version from a character's experience still requires an
        accepted cognition event. Creating this artifact changes no selection.
        """
        validate_contract("WorldRef", world_ref)
        if prior_ref is not None:
            validate_contract("ArtifactRef", prior_ref)
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 200,
                "INVALID_ARGUMENT", "A bounded operation key is required")
        require(isinstance(body, str) and 0 < len(body) <= 2_000_000,
                "INVALID_ARGUMENT", "A message must contain bounded text")
        encoded = body.encode("utf-8")
        require(len(encoded) <= 2_000_000, "CAPACITY_LIMIT", "Message exceeds artifact limit")
        wid, bid = world_ref["work_id"], world_ref["branch_id"]
        request = {"world_ref": world_ref, "prior_ref": prior_ref, "body_sha256": sha256(encoded).hexdigest()}
        kind = "MESSAGE_SOURCE_VERSION"

        def validate(repo):
            self.kernel.authorize(repo, principal, wid, "ADMIN", bid)
            self.kernel.revision(repo, world_ref)
            if prior_ref is not None:
                # A generic TEXT ref cannot be used to join unrelated artifact families.
                self.kernel.artifact(repo, prior_ref, wid, kind="TEXT")
                family = repo.conn.execute(text("""SELECT branch_id FROM nf_receipts
                    WHERE work_id=:work AND command_type='MESSAGE_SOURCE_VERSION'
                    AND payload->'source_ref'->>'artifact_revision_id'=:revision"""),
                    {"work": wid, "revision": prior_ref["artifact_revision_id"]}).scalar_one_or_none()
                require(family is not None, "INVALID_EVIDENCE", "Prior message source unavailable")
                self.kernel.authorize(repo, principal, wid, "ADMIN", str(family))
            return self.kernel._prior(repo, principal, wid, kind, idempotency_key, request)

        with self.kernel.db.uow(write=False) as repo:
            existing = validate(repo)
            if existing:
                return existing
        with self.kernel.objects.lock():
            ref = self.kernel._blob(encoded)
            if prior_ref:
                ref["artifact_id"] = prior_ref["artifact_id"]
            with self.kernel.db.uow() as repo:
                # Serialize retries of this operation only. Different revisions can coexist.
                repo.conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                  {"key": f"message-version:{wid}:{principal.principal_id}:{idempotency_key}"})
                existing = validate(repo)
                if existing:
                    return existing
                result = {"source_ref": ref, "prior_ref": prior_ref, "created_at_world_ref": world_ref}
                self.kernel._register_artifact(repo, ref, wid, encoded, "TEXT", principal.principal_id)
                self.kernel._receipt(repo, principal, world_ref, kind, idempotency_key, request, result)
                return result
