"""Bind immutable operator profiles to the existing NF attempt and host journal."""
from agent_runtime.providers import ProfileError, invoke, read_profiles
from novel_factory.domain.errors import DomainError


class RuntimeModelAdapter:
    def __init__(self, profile, journal):
        self.profile, self.journal = profile, journal
        self.provider_key, self.account_key = profile.provider_key, profile.account_key

    @property
    def configuration(self):
        return self.profile.describe()

    def check_available(self, *, dry_run=False):
        if dry_run != bool(getattr(self.journal, "dry_run", False)):
            raise DomainError("DRY_RUN_MODE_MISMATCH", "Runtime and request execution modes differ")
        if dry_run:
            return
        try:
            self.profile.preflight()
        except ProfileError as exc:
            raise DomainError(str(exc), "The selected model is unavailable for this dispatch") from None

    def generate(self, request):
        from agent_runtime.protocol import ModelCallFailure
        if request.dry_run != bool(getattr(self.journal, "dry_run", False)):
            raise ModelCallFailure("DRY_RUN_MODE_MISMATCH", sent=False)
        try:
            receipt_target = None
            if self.profile.kind == "deepseek" and not request.dry_run:
                from .wire_receipts import destination
                receipt_target = {"attempt_id": request.attempt_id,
                                  "path": destination(self.journal.kernel.objects, request.attempt_id)}
            response = invoke(self.profile, request, authorize=self.journal.authorize_send,
                              record=self.journal.record, receipt_target=receipt_target)
            if response.dry_run != request.dry_run:
                raise ModelCallFailure("DRY_RUN_MODE_MISMATCH")
            return response
        except ProfileError as exc:
            raise DomainError(str(exc), "The selected model is unavailable for this dispatch") from None


def load_runtime_profiles(path, journal):
    try:
        return {key: RuntimeModelAdapter(value, journal) for key, value in read_profiles(path).items()}
    except ProfileError as exc:
        raise DomainError(str(exc), "Invalid runtime provider catalog") from None
