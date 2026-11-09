"""Backend seam: start fields, execution/public events, and session control.

Adapters run under a single claimed turn. They emit only normalized public
events and return a settlement; ownership, turns and reservations stay shared.
A backend need not use ACPX. Register it here and ship its module with the runner.
"""
from __future__ import annotations
from dataclasses import dataclass
try:
    from .acpx_backend import AcpxBackend
    from .agy import AgyBackend
except ImportError:
    from acpx_backend import AcpxBackend
    from agy import AgyBackend


@dataclass(frozen=True)
class TurnResult:
    state: str
    exit_code: int | None
    stop_reason: str | None
    note: str | None = None


BACKENDS = {name: AcpxBackend() for name in ('omp', 'codex', 'pi')}
BACKENDS['agy'] = AgyBackend()


def backend_for(agent):
    try:
        return BACKENDS[agent]
    except KeyError:
        raise ValueError('Unknown backend: '+str(agent)) from None


def prepare_start(agent, model=None, effort=None, agy_approval=None):
    return backend_for(agent).prepare_start(model, effort, agy_approval)
