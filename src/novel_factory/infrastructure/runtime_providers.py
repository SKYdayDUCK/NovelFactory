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

    def check_available(self):
        try:
            self.profile.preflight()
        except ProfileError as exc:
            raise DomainError(str(exc), "The selected model is unavailable for this dispatch") from None

    def generate(self, request):
        try:
            return invoke(self.profile, request, authorize=self.journal.authorize_send, record=self.journal.record)
        except ProfileError as exc:
            raise DomainError(str(exc), "The selected model is unavailable for this dispatch") from None


def load_runtime_profiles(path, journal):
    try:
        return {key: RuntimeModelAdapter(value, journal) for key, value in read_profiles(path).items()}
    except ProfileError as exc:
        raise DomainError(str(exc), "Invalid runtime provider catalog") from None
