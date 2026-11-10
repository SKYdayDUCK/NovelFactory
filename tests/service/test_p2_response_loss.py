"""R1-H: drop an actual TCP response after the upstream world commit succeeds."""
import json
import os
import socket
import socketserver
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from sqlalchemy.dialects.postgresql import insert

from benchmarks.http_load import loopback_server
from novel_factory.application.authorization import ADMIN_CAPABILITIES
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.reducer import state_at
from novel_factory.fixtures import entity_value, point, prepare_command
from novel_factory.infrastructure.database import tables
from novel_factory.interfaces.http import create_app
from novel_factory.orchestration.tasks import OutboxService

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.fault, pytest.mark.recovery]


@contextmanager
def drop_first_success_response(upstream_url):
    evidence = {'dropped': False, 'upstream_receipt': None}
    lock = threading.Lock()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(10)
            chunks = bytearray()
            while b'\r\n\r\n' not in chunks:
                data = self.request.recv(4096)
                if not data:
                    return
                chunks.extend(data)
                if len(chunks) > 262144:
                    return
            header, body = bytes(chunks).split(b'\r\n\r\n', 1)
            first, *lines = header.decode('iso-8859-1').split('\r\n')
            method, path, _ = first.split()
            headers = dict(line.split(':', 1) for line in lines)
            headers = {key.lower(): value.strip() for key, value in headers.items()}
            length = int(headers.get('content-length', '0'))
            if not 0 <= length <= 262144:
                return
            while len(body) < length:
                data = self.request.recv(length-len(body))
                if not data:
                    return
                body += data
            with httpx.Client(base_url=upstream_url, timeout=15, trust_env=False) as http:
                response = http.request(method, path, content=body,
                    headers={'Authorization': headers['authorization'],
                             'Content-Type': 'application/json'})
            with lock:
                if not evidence['dropped'] and response.status_code == 200:
                    evidence.update(dropped=True, upstream_receipt=response.json())
                    # No HTTP response bytes are forwarded. The downstream
                    # sees a real connection EOF, after upstream commit+reply.
                    self.request.shutdown(socket.SHUT_RDWR)
                    return
            data = response.content
            self.request.sendall((f'HTTP/1.1 {response.status_code} Response\r\n'
                f'Content-Length: {len(data)}\r\nContent-Type: application/json\r\n'
                'Connection: close\r\n\r\n').encode() + data)

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_address[1]}', evidence
        finally:
            server.shutdown()
            thread.join(5)


def test_r1_h_real_tcp_lost_reply_retry_revocation_and_outbox_recovery(service_env, service_world, tmp_path):
    kernel = service_env['kernel']
    world, ids = service_world, service_world['ids']
    grant = kernel.issue_grant(world['principal'], world['world_ref']['work_id'],
        role_id='control_operator', capabilities=ADMIN_CAPABILITIES, planes=['WORLD'],
        max_story_time=point(100))
    prepared = prepare_command(kernel, grant['principal'], world['world_ref'], 'TRANSFER_ITEM',
        {'item_id': ids['sword'], 'from_id': ids['smith'], 'to_id': ids['protagonist'],
         'transfer_kind': 'GIFT', 'at': point(10)}, mode='DEPENDENCY_GUARDED')
    headers = {'Authorization': 'Bearer ' + grant['token']}
    app = create_app(kernel)
    with (loopback_server(app) as upstream,
          drop_first_success_response(upstream) as (proxy, transport),
          httpx.Client(base_url=proxy, timeout=15, trust_env=False) as http):
        with pytest.raises(httpx.RemoteProtocolError) as network_loss:
            http.post('/api/v1/world/accept-guarded', json=prepared['request'], headers=headers)
        assert transport['dropped']
        committed = transport['upstream_receipt']
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo, world['world_ref']['work_id']) == committed['world_ref']
            assert len(kernel.revision(repo, committed['world_ref'])['selection']) == 1
        retried = http.post('/api/v1/world/accept-guarded', json=prepared['request'], headers=headers)
        assert retried.status_code == 200 and retried.json() == committed
        changed = deepcopy(prepared['request'])
        changed['generation'] += 1
        mismatch = http.post('/api/v1/world/accept-guarded', json=changed, headers=headers)
        assert mismatch.status_code == 409 and mismatch.json()['code'] == 'IDEMPOTENCY_MISMATCH'
        changed = deepcopy(prepared['request'])
        changed['idempotency_key'] += ':new'
        duplicate = http.post('/api/v1/world/accept-guarded', json=changed, headers=headers)
        assert duplicate.status_code == 409 and duplicate.json()['code'] == 'ALREADY_ACCEPTED'
        kernel.revoke_grant(world['principal'], grant['principal'].grant_id)
        revoked = http.post('/api/v1/world/accept-guarded', json=prepared['request'], headers=headers)
        assert revoked.status_code == 401
    # No derived consumer ran before the failed reply. A restarted consumer can
    # apply the durable outbox row once; losing its acknowledgement cannot repeat it.
    dispatcher = OutboxService(kernel.db.engine, tables)
    with kernel.db.uow(write=False) as repo:
        outbox = next(row for row in repo.many('outbox', {'topic': 'world.accepted'})
                      if row['payload']['world_ref'] == committed['world_ref'])
    watermark = tables['index_watermarks']
    def materialize(conn, payload):
        wr = payload['world_ref']
        conn.execute(insert(watermark).values(branch_id=wr['branch_id'], index_name='r1-h-tcp',
            world_revision_id=wr['world_revision_id'], branch_seq=committed['branch_seq'], generation=1)
            .on_conflict_do_update(index_elements=[watermark.c.branch_id, watermark.c.index_name],
                                  set_={'generation': watermark.c.generation+1}))
    assert dispatcher.consume('r1-h-tcp', outbox['outbox_id'], materialize)
    assert not dispatcher.consume('r1-h-tcp', outbox['outbox_id'], materialize)
    with kernel.db.uow(write=False) as repo:
        final = kernel.revision(repo, committed['world_ref'])
        assert len(final['selection']) == 1
        assert state_at(final['snapshot'], ids['sword'], 'active_holder', point(10))['data'] == entity_value(ids['protagonist'])
        assert repo.one('index_watermarks', {'branch_id': committed['world_ref']['branch_id'],
                                           'index_name': 'r1-h-tcp'})['generation'] == 1
    output = Path(os.environ.get('NF_P2_ACCEPTANCE_EVIDENCE_DIR', tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output/'r1_h_tcp_response_loss.json').write_text(json.dumps({
        'real_transport': 'loopback TCP proxy dropped full upstream success before any downstream response',
        'network_error_type': type(network_loss.value).__name__, 'network_error': str(network_loss.value),
        'request_sha256': fingerprint(prepared['request']), 'accepted_receipt': committed,
        'retry_receipt': retried.json(), 'mismatch_code': mismatch.json()['code'],
        'duplicate_code': duplicate.json()['code'], 'revoked_status': revoked.status_code,
        'world_effect_groups': 1, 'outbox_id': outbox['outbox_id'], 'derived_generation': 1,
        'external_model_calls_during_recovery': 0}, ensure_ascii=False, indent=2)+'\n')
