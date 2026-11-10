"""Stable, transport-independent errors. Messages must be safe for the caller."""

class DomainError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, recovery: str = "NONE"):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.recovery = recovery

    def as_dict(self, request_id: str) -> dict:
        return {"code": self.code, "safe_message": self.safe_message,
                "retryable": self.retryable, "request_id": request_id,
                "recovery": self.recovery}
