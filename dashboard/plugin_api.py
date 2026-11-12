"""Operator-only release surface; no model tool, caller-selected target or credentials."""
import importlib.util
import hashlib
from pathlib import Path
import sys

from fastapi import APIRouter, HTTPException, Request
from hermes_cli.web_deps import late

# Dashboard APIs are loaded by file, outside the plugin package in both host modes.
_root = Path(__file__).resolve().parents[1]
_name = '_call_cli_release_' + hashlib.sha256(str(_root).encode()).hexdigest()[:16]
if _name not in sys.modules:
    spec = importlib.util.spec_from_file_location(_name, _root / 'release.py',
                                                 submodule_search_locations=[str(_root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_name, None)
        raise
Release = sys.modules[_name].Release
NativeBackend = sys.modules[_name].NativeBackend
router = APIRouter()


def service(request):
    late('_require_token')(request)
    from hermes_constants import get_default_hermes_root, get_hermes_home
    home = Path(get_hermes_home()).resolve()
    root = Path(get_default_hermes_root(home=home)).resolve()
    if home != root or any(k != 'profile' or v != 'default' for k,v in request.query_params.multi_items()):
        raise HTTPException(404, 'Release policy not available in this profile')
    try:
        policy = Release.load_policy(home)
        return Release(home, policy, NativeBackend())
    except (OSError, ValueError, PermissionError):
        raise HTTPException(409, 'Private single-target policy unavailable') from None


@router.post('/release/prepare')
async def prepare(request: Request):
    release = service(request)
    if await request.body() not in (b'', b'{}'):
        raise HTTPException(400, 'Prepare accepts no target or approval fields')
    try:
        return release.prepare()
    except (ValueError, OSError):
        raise HTTPException(409, 'Target snapshot unavailable or mismatched') from None


@router.get('/release/receipt')
def receipt(request: Request):
    return {'receipt': service(request).receipt()}

@router.post('/release/reconcile')
async def reconcile(request: Request):
    release = service(request)
    if await request.body() not in (b'', b'{}'):
        raise HTTPException(400, 'Reconcile accepts no target fields')
    try:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(release.reconcile)
    except (ValueError, OSError):
        raise HTTPException(409, 'Reconciliation unavailable; reservation retained') from None


@router.post('/release')
async def execute(request: Request):
    release = service(request)
    try:
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {'nonce', 'operation'}
                or body['operation'] != 'release-only' or not isinstance(body['nonce'], str)
                or not 1 <= len(body['nonce']) <= 128):
            raise ValueError('Invalid confirmation')
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(release.release, body['nonce'])
    except (ValueError, OSError):
        raise HTTPException(409, 'Release confirmation refused; inspect receipt') from None
