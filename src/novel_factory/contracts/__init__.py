"""Schema-backed shared contracts. No module owns an independent WorldRef."""
from .registry import validate_contract, schema_for, model_for

__all__ = ["validate_contract", "schema_for", "model_for"]
