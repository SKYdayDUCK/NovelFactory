"""Stable application import for the deterministic world reducer."""
from .world import (replay, state_at, resource_at, lifecycle_at, resource_key,
                    INTERPRETER_VERSION, PROJECTION_VERSION, PREDICATES)

__all__ = ['replay', 'state_at', 'resource_at', 'lifecycle_at', 'resource_key',
           'INTERPRETER_VERSION', 'PROJECTION_VERSION', 'PREDICATES']
