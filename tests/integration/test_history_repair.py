"""R1-B: audited history replacement through the real PostgreSQL commit path.

No test writes an event, projection, approval or branch pointer directly.
Read-only repository inspections verify immutable identities and audit records.
"""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.domain.world import state_at
from novel_factory.fixtures import (
    create_bound_task, entity_value, load_fixture, point, prepare_command, run_command, seed_sword_history,
)

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _revision(kernel, wr):
    with kernel.db.uow(write=False) as repo:
        return kernel.revision(repo, wr)


def _reviewed_gift_repair(kernel):
    fixture = seed_sword_history(kernel, load_fixture(kernel, 'r1_sword'))
    principal = fixture['principal']; old = fixture['world_ref']
    loan_ref = fixture['loan']['receipt']['accepted_event_refs'][0]
    with kernel.db.uow(write=False) as repo:
        old_event = repo.one('events', {'event_version_id': loan_ref['event_version_id']})['payload']
    replacement = deepcopy(old_event)
    replacement['event_version_id'] = str(uuid4())
    replacement['representation_id'] = str(uuid4())
    replacement['effects'].append({
        'kind': 'RELATION_SET', 'effect_key': 'repair:gift:legal_owner',
        'subject_id': fixture['ids']['sword'], 'predicate': 'legal_owner',
        'object_id': fixture['ids']['friend'], 'value_status': 'VALUE',
    })
    branch = kernel.fork(principal, old, 'REPAIR', 'r1-b:repair-fork')
    reviewer = kernel.issue_grant(
        principal, old['work_id'], role_id='control_operator',
        branch_ids=[branch['branch_id']], capabilities=['READ', 'APPROVE', 'REPAIR'],
        planes=['WORLD'], max_story_time=point(100),
    )['principal']
    repaired = run_command(
        kernel, principal, branch, 'REPAIR_EVENT',
        {'occurrence_id': loan_ref['occurrence_id'], 'replacement_event': replacement,
         'reason': 'R1-B: D20 is an intentional gift, preserving the same physical handover.'},
        reviewer=reviewer, key='r1-b:loan-to-gift',
    )
    return {**fixture, 'old': old, 'fork': branch, 'old_loan_event': old_event,
            'replacement': replacement, 'reviewer': reviewer, 'repair': repaired}


def _request(task, wr, sword_id, *, plane='WORLD', latest=False):
    revision = ({'mode': 'LATEST_AT_START', 'branch_selector': {'kind': 'CANON', 'work_id': wr['work_id']}}
                if latest else {'mode': 'FIXED', 'world_ref': wr})
    cut = {'plane': plane}
    return {
        'schema_version': '2.1', 'task_id': task['task_id'], 'generation': task['generation'],
        'read_contract': {'revision': revision, 'process_policy': 'REQUIRE_EVALUATED',
                          'index_policy': 'AUTHORITATIVE', 'max_wait_ms': 0},
        'knowledge_cut': cut, 'subject_time': {'kind': 'POINT', 'at': point(25)},
        'query': {'kind': 'STATE', 'subject_ids': [sword_id], 'predicates': ['owner', 'active_holder']},
        'page_size': 100, 'cursor': None,
    }


def test_r1_b_reviewed_loan_to_gift_preserves_history_and_promotes(kernel_env):
    kernel = kernel_env['kernel']; f = _reviewed_gift_repair(kernel)
    principal = f['principal']; old = f['old']; new = f['repair']['world_ref']; ids = f['ids']
    before = _revision(kernel, old); after = _revision(kernel, new)
    occurrence = f['replacement']['occurrence_id']
    assert before['selection'][occurrence] == f['old_loan_event']['event_version_id']
    assert after['selection'][occurrence] == f['replacement']['event_version_id']
    assert f['old_loan_event']['effect_group_id'] == f['replacement']['effect_group_id']
    assert len(before['selection']) == len(after['selection']) == len(after['snapshot']['events'])
    assert state_at(before['snapshot'], ids['sword'], 'owner', point(25))['data'] == entity_value(ids['protagonist'])
    assert state_at(after['snapshot'], ids['sword'], 'owner', point(25))['data'] == entity_value(ids['friend'])
    for snapshot in (before['snapshot'], after['snapshot']):
        assert state_at(snapshot, ids['sword'], 'active_holder', point(25))['data'] == entity_value(ids['friend'])
        assert state_at(snapshot, ids['sword'], 'active_use', point(41))['value_status'] == 'NOT_APPLICABLE'
    assert before['snapshot']['resources'] == after['snapshot']['resources']
    assert before['snapshot']['resource_history'] == after['snapshot']['resource_history']

    # R1-B-03: a changed owner does not delete or silently rewrite retained
    # cognition. The old precise evidence remains historical provenance, not
    # a forged reference to the newly selected representation.
    assert before['snapshot']['cognitions'] == after['snapshot']['cognitions']
    cognition = after['snapshot']['cognitions'][0]
    assert cognition['evidence_refs'] == [{
        'kind': 'EVENT', 'world_ref': f['loan']['world_ref'],
        'event_ref': f['loan']['receipt']['accepted_event_refs'][0],
    }]
    assert state_at(after['snapshot'], ids['sword'], 'active_holder', point(35),
                    ids['protagonist'], point(35))['data'] == entity_value(ids['friend'])

    comparison = kernel.compare(principal, old, new)
    assert comparison['event_changes'] == [{
        'occurrence_id': occurrence, 'left_event_version': f['old_loan_event']['event_version_id'],
        'right_event_version': f['replacement']['event_version_id'],
    }]
    assert comparison['left_state_hash'] != comparison['right_state_hash']
    impact = kernel.impact(principal, new, ['ENTITY:' + ids['sword']])
    assert any(item['stale'] and item['owner_type'] == 'context' for item in impact)
    assert any(item['stale'] and item['owner_type'] == 'proposal' for item in impact)

    with pytest.raises(DomainError) as self_review:
        kernel.approve(principal, f['repair']['proposal']['proposal_ref'], old['work_id'],
                       idempotency_key='r1-b:self-review-is-forbidden')
    assert self_review.value.code == 'VIEW_DENIED'
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, old['work_id']) == old
        record = repo.one('events', {'event_version_id': f['old_loan_event']['event_version_id']})
        assert fingerprint(record['payload']) == fingerprint(f['old_loan_event'])
        approval = repo.one('approvals', {'approval_id': f['repair']['request']['approval_refs'][0]})
        assert approval['payload']['principal_id'] == f['reviewer'].principal_id != principal.principal_id

    task = create_bound_task(kernel, principal, old, point(60), [ids['sword']])
    service = WorldQueryService(kernel)
    fixed = _request(task, old, ids['sword'])
    assert service.query(principal, fixed)['payload']['state_items'][0]['answer']['data'] == entity_value(ids['protagonist'])
    assert kernel.promote(principal, old, new, 'r1-b:promote') == new
    assert kernel.promote(principal, old, new, 'r1-b:promote') == new
    latest = service.query(principal, _request(task, old, ids['sword'], latest=True))
    assert latest['read_cut']['world_ref'] == new
    assert latest['payload']['state_items'][0]['answer']['data'] == entity_value(ids['friend'])
    historical = service.query(principal, fixed)
    assert historical['read_cut']['world_ref'] == old
    assert historical['payload']['state_items'][0]['answer']['data'] == entity_value(ids['protagonist'])
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, old['work_id']) == new
        promotions = repo.many('promotions', {'work_id': old['work_id']})
        assert len(promotions) == 1
        report = kernel.artifact(repo, promotions[0]['report_ref'], old['work_id'], kind='REVALIDATION_REPORT')['payload']
        assert report['deterministic_replay'] == 'PASS'
        assert report['prerequisite_revalidation']['closed_command_prerequisites'] == 'PASS'
        retained = report['prerequisite_revalidation']['retained_cognition_sources']
        assert any(item['source_world_ref'] == f['loan']['world_ref']
                   and item['event_ref'] == f['loan']['receipt']['accepted_event_refs'][0]
                   and item['current_selected_version'] == f['replacement']['event_version_id']
                   and item['disposition'] == 'HISTORICAL_PROVENANCE_ONLY' for item in retained)

    # Keeping an immutable old source for auditing is not permission to use
    # that superseded occurrence version as fresh evidence in the new history.
    with pytest.raises(DomainError) as invalid_evidence:
        run_command(kernel, principal, new, 'RECORD_COGNITION', {
            'actor_id': ids['protagonist'], 'record_kind': 'BELIEF', 'epistemic_mode': 'HEARD',
            'statement': encode_proposition(ids['sword'], 'possessed_by', entity_value(ids['friend'])),
            'evidence_refs': cognition['evidence_refs'], 'at': point(50),
        }, key='r1-b:old-evidence-cannot-be-new-proof')
    assert invalid_evidence.value.code == 'INVALID_EVIDENCE'
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, old['work_id']) == new


@pytest.mark.parametrize('changed_branch', ['canon', 'candidate'])
def test_r1_b_promotion_compares_both_canon_and_candidate_heads(kernel_env, changed_branch):
    kernel = kernel_env['kernel']; f = _reviewed_gift_repair(kernel)
    principal = f['principal']; old = f['old']; candidate = f['repair']['world_ref']
    source = old if changed_branch == 'canon' else candidate
    advanced = run_command(kernel, principal, source, 'ADVANCE_SCOPE',
                           {'scope_id': f['ids']['sword'], 'target': point(43)},
                           key='r1-b:advance-before-promotion')
    with pytest.raises(DomainError) as error:
        kernel.promote(principal, old, candidate, 'r1-b:stale-promotion')
    assert error.value.code == ('HEAD_CONFLICT' if changed_branch == 'canon' else 'REPAIR_INCOMPLETE')
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, old['work_id']) == (advanced['world_ref'] if changed_branch == 'canon' else old)
        assert kernel.head(repo, source['work_id'], source['branch_id']) == advanced['world_ref']
        assert not repo.many('promotions', {'work_id': old['work_id']})


def test_r1_b_promotion_rechecks_current_independent_review_authority(kernel_env):
    kernel = kernel_env['kernel']; f = _reviewed_gift_repair(kernel)
    kernel.revoke_grant(f['principal'], f['reviewer'].grant_id)
    with pytest.raises(DomainError) as error:
        kernel.promote(f['principal'], f['old'], f['repair']['world_ref'], 'r1-b:revoked-reviewer')
    assert error.value.code == 'APPROVAL_STALE'
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, f['old']['work_id']) == f['old']
        assert not repo.many('promotions', {'work_id': f['old']['work_id']})


def test_ordinary_approval_and_revocation_respect_actual_branch_grant(kernel_env):
    kernel = kernel_env['kernel']; f = load_fixture(kernel)
    principal = f['principal']; canon = f['world_ref']; work_id = canon['work_id']
    candidate = kernel.fork(principal, canon, 'CANDIDATE', 'approval-scope:candidate')
    args = {'item_id': f['ids']['sword'], 'at': point(10)}
    local = prepare_command(kernel, principal, canon, 'DESTROY_ITEM', args, key='approval-scope:local')
    remote = prepare_command(kernel, principal, candidate, 'DESTROY_ITEM', args, key='approval-scope:remote')
    reviewer = kernel.issue_grant(
        principal, work_id, role_id='control_operator', branch_ids=[canon['branch_id']],
        capabilities=['READ', 'APPROVE'], planes=['WORLD'], max_story_time=point(60),
    )['principal']

    # Positive control: the actual stored grant has valid approval authority
    # on canon. Its lack of another branch is what must deny the other calls.
    local_approval = kernel.approve(reviewer, local['proposal']['proposal_ref'], work_id,
                                    idempotency_key='approval-scope:permitted-review')
    kernel.revoke_approval(reviewer, work_id, local_approval)
    remote_approval = remote['proposal']['approval_id']
    with kernel.db.uow(write=False) as repo:
        protected = repo.one('approvals', {'approval_id': remote_approval})
        assert protected['active'] is True

    with pytest.raises(DomainError) as approval_denied:
        kernel.approve(reviewer, remote['proposal']['proposal_ref'], work_id,
                       idempotency_key='approval-scope:forbidden-review')
    assert approval_denied.value.code == 'VIEW_DENIED'
    with pytest.raises(DomainError) as revocation_denied:
        kernel.revoke_approval(reviewer, work_id, remote_approval)
    assert revocation_denied.value.code == 'VIEW_DENIED'
    with kernel.db.uow(write=False) as repo:
        assert repo.one('approvals', {'approval_id': local_approval})['active'] is False
        unchanged = repo.one('approvals', {'approval_id': remote_approval})
        assert unchanged['active'] is True and unchanged['epoch'] == protected['epoch']
        assert not repo.many('approvals', {'proposal_id': unchanged['proposal_id'], 'grant_id': reviewer.grant_id})
        assert not repo.one('receipts', {'work_id': work_id, 'principal_id': reviewer.principal_id,
                                        'command_type': 'APPROVE', 'idempotency_key': 'approval-scope:forbidden-review'})
        assert kernel.head(repo, work_id) == canon
        assert kernel.head(repo, work_id, candidate['branch_id']) == candidate
