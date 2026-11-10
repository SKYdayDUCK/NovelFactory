"""Real process death/restart preserves commit receipts and authorization."""
from copy import deepcopy

import httpx
import pytest

from benchmarks.process_server import ServiceProcesses
from novel_factory.application.authorization import ADMIN_CAPABILITIES
from novel_factory.fixtures import point, prepare_command

from .test_p2_response_loss import drop_first_success_response

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.fault,
              pytest.mark.recovery]


def test_spawned_workers_restart_after_lost_reply_without_reapplying_world(service_env, service_world):
    kernel, world = service_env['kernel'], service_world
    ids = world['ids']
    issued = kernel.issue_grant(world['principal'], world['world_ref']['work_id'],
        role_id='control_operator', capabilities=ADMIN_CAPABILITIES, planes=['WORLD'],
        max_story_time=point(100))
    prepared = prepare_command(kernel, issued['principal'], world['world_ref'], 'TRANSFER_ITEM',
        {'item_id': ids['sword'], 'from_id': ids['smith'], 'to_id': ids['protagonist'],
         'transfer_kind': 'GIFT', 'at': point(10)}, mode='DEPENDENCY_GUARDED')
    headers = {'Authorization': 'Bearer ' + issued['token']}
    with ServiceProcesses(service_env['url'], kernel.objects.root, processes=2) as service:
        with drop_first_success_response(service.base_url) as (proxy, evidence):
            with (httpx.Client(base_url=proxy, timeout=15, trust_env=False) as http,
                  pytest.raises(httpx.RemoteProtocolError)):
                http.post('/api/v1/world/accept-guarded', json=prepared['request'], headers=headers)
            assert evidence['dropped']
            committed = evidence['upstream_receipt']
        snapshot = service.snapshot()
        assert snapshot['database']['configured_pool_size'] == 8
        assert snapshot['database']['checked_out_at_end'] == 0
        # Real processes are terminated after upstream commit and before the
        # client has obtained any successful receipt. No exception-only mock.
        for child in service.children:
            child.terminate()
            child.join(5)
            assert not child.is_alive()
    with (ServiceProcesses(service_env['url'], kernel.objects.root, processes=2) as restarted,
          httpx.Client(base_url=restarted.base_url, timeout=15, trust_env=False) as http):
        recovered = http.post('/api/v1/world/accept-guarded', json=prepared['request'], headers=headers)
        assert recovered.status_code == 200 and recovered.json() == committed
        changed = deepcopy(prepared['request'])
        changed['generation'] += 1
        stale = http.post('/api/v1/world/accept-guarded', json=changed, headers=headers)
        assert stale.status_code == 409 and stale.json()['code'] == 'IDEMPOTENCY_MISMATCH'
        kernel.revoke_grant(world['principal'], issued['principal'].grant_id)
        denied = http.post('/api/v1/world/accept-guarded', json=prepared['request'], headers=headers)
        assert denied.status_code == 401
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, world['world_ref']['work_id']) == committed['world_ref']
        assert len(kernel.revision(repo, committed['world_ref'])['selection']) == 1
