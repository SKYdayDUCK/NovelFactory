"""Bounded load-driver retries against a real post-commit TCP disconnect."""
import asyncio
from copy import deepcopy

import httpx
import pytest

from benchmarks.http_load import loopback_server
from benchmarks.p2_behavioral_load import retry_same_request, retryable_failure
from novel_factory.application.authorization import ADMIN_CAPABILITIES
from novel_factory.fixtures import point, prepare_command
from novel_factory.interfaces.http import create_app
from .test_p2_response_loss import drop_first_success_response

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.fault, pytest.mark.recovery]


def test_bounded_driver_replays_same_bytes_after_real_commit_response_loss(service_env, service_world):
    kernel, world = service_env['kernel'], service_world
    ids = world['ids']
    grant = kernel.issue_grant(world['principal'], world['world_ref']['work_id'],
        role_id='control_operator', capabilities=ADMIN_CAPABILITIES, planes=['WORLD'],
        max_story_time=point(100))
    prepared = prepare_command(kernel, grant['principal'], world['world_ref'], 'TRANSFER_ITEM',
        {'item_id': ids['sword'], 'from_id': ids['smith'], 'to_id': ids['protagonist'],
         'transfer_kind': 'GIFT', 'at': point(10)}, mode='DEPENDENCY_GUARDED')
    headers = {'Authorization': 'Bearer ' + grant['token'], 'Content-Type': 'application/json'}
    sent, attempts = [], []

    async def run(proxy, payload):
        async with httpx.AsyncClient(base_url=proxy, timeout=15, trust_env=False) as http:
            async def once(data, attempt):
                sent.append(data)
                try:
                    response = await http.post('/api/v1/world/accept-guarded', content=data, headers=headers)
                    body = response.json()
                    row = {'status': response.status_code, 'code': body.get('code'),
                           'retryable': retryable_failure(response.status_code, body)}
                    attempts.append(row)
                    return (body if response.status_code == 200 else None), row
                except httpx.TransportError as exc:
                    row = {'status': 'transport_error', 'error_type': type(exc).__name__, 'retryable': True}
                    attempts.append(row)
                    return None, row
            return await retry_same_request(once, payload, max_attempts=3)

    with loopback_server(create_app(kernel)) as upstream, drop_first_success_response(upstream) as (proxy, dropped):
        recovered = asyncio.run(run(proxy, prepared['request']))
        assert dropped['dropped'] and recovered == dropped['upstream_receipt']
        assert [row['status'] for row in attempts] == ['transport_error', 200]
        assert sent[0] == sent[1]
        with kernel.db.uow(write=False) as repo:
            revision = kernel.revision(repo, recovered['world_ref'])
            assert revision['branch_seq'] == 1 and len(revision['selection']) == 1
        # New generation under the same idempotency identity is a hard rejection;
        # the recovery driver must not change keys or try to refresh the command.
        changed = deepcopy(prepared['request'])
        changed['generation'] += 1
        attempts.clear()
        assert asyncio.run(run(proxy, changed)) is None
        assert len(attempts) == 1 and attempts[0]['code'] == 'IDEMPOTENCY_MISMATCH'
        kernel.revoke_grant(world['principal'], grant['principal'].grant_id)
        attempts.clear()
        assert asyncio.run(run(proxy, prepared['request'])) is None
        assert len(attempts) == 1 and attempts[0]['status'] == 401
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, world['world_ref']['work_id']) == recovered['world_ref']
        assert len(kernel.revision(repo, recovered['world_ref'])['selection']) == 1
