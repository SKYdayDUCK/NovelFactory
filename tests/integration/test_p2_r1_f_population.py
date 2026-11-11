"""R1-F: 200 persistent slots, 20 independent commands and 20 sealed intents.

Joint arbitration keeps its strict common-start rule. If independent commits
advance HEAD first, old joint input is rejected and explicitly requalified; this
test documents that conservative boundary instead of silently moving W0.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from novel_factory.application.queries import WorldQueryService
from novel_factory.application.simulation import WorldSchedulerService, outcome_command
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reducer import state_at
from novel_factory.domain.world import resource_at
from novel_factory.fixtures import (
    create_bound_task,
    entity_value,
    fixture_definition,
    point,
    prepare_command,
    run_command,
)

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.concurrency]


def test_r1_f_200_slots_20_independent_and_20_intents_require_explicit_joint_requalification(kernel_env, tmp_path):
    kernel = kernel_env['kernel']
    definition = fixture_definition('white_bridge')
    genesis, ids = deepcopy(definition['genesis']), definition['ids']
    actors = [str(uuid4()) for _ in range(20)]
    accounts = [[str(uuid4()), str(uuid4())] for _ in range(20)]
    for index, actor in enumerate(actors):
        genesis['entities'][actor] = {'entity_id': actor, 'entity_kind': 'CHARACTER',
            'name': f'TEST_FIXTURE_争剑者{index}', 'created_at': point(1)}
    for index, pair in enumerate(accounts):
        for account, amount in zip(pair, ['100', '0']):
            genesis['entities'][account] = {'entity_id': account, 'entity_kind': 'ACCOUNT',
                'name': f'TEST_FIXTURE_独立仓储{index}', 'created_at': point(1)}
            genesis['resources'].append({'account_id': account, 'resource_id': ids['grain'],
                'quantity': amount, 'unit': 'bag'})
    world = kernel.bootstrap_work('TEST_FIXTURE:R1-F 200 slots', genesis,
        frontiers={eid: point(1, 1) for eid in genesis['entities']})
    principal = world['principal']
    scheduler = WorldSchedulerService(kernel)
    with kernel.db.uow(write=False) as repo:
        rule_refs = [{'kind': 'RULE', 'artifact_ref':
            repo.one('works', {'work_id': world['world_ref']['work_id']})['rules_ref']}]
    process_id = str(uuid4())
    process = {'schema_version': '2.2', 'process_id': process_id, 'process_kind': 'INTERACTION',
        'scope_ids': [ids['sword']], 'participant_ids': actors, 'required_actor_ids': actors,
        'start_at': point(2), 'due_at': point(10), 'world_stage': 'ACTIVE', 'high_impact': True,
        'channels': [], 'goal': '二十角色分别争取持剑；固定规则裁决不采用到达顺序。',
        'evaluator_kind': 'SEMANTIC', 'rule_evidence_refs': rule_refs,
        'trigger_id': str(uuid4()), 'trigger_version_id': str(uuid4())}
    process_ref = scheduler.stage_process(principal, world['world_ref'], process, 'register-process')
    registration = run_command(kernel, principal, world['world_ref'], 'REGISTER_PROCESS',
        {'at': point(2), 'process': process, 'record_ref': process_ref})
    base = registration['world_ref']
    grants = {actor: kernel.issue_grant(principal, base['work_id'], role_id='character_actor',
        actor_id=actor, capabilities=['READ', 'COMPILE', 'TASK_CREATE', 'TASK_CLAIM', 'ARTIFACT_WRITE'],
        planes=['BELIEF'], max_story_time=point(10)) for actor in actors}
    readers = [create_bound_task(kernel, principal, base, point(10), [ids['sword']], ['owner'])
               for _ in range(160)]
    commands = [prepare_command(kernel, principal, base, 'RESOURCE_TRANSFER',
        {'from_account': pair[0], 'to_account': pair[1], 'resource_id': ids['grain'],
         'quantity': '1', 'unit': 'bag', 'at': point(10)}, mode='DEPENDENCY_GUARDED',
        task=create_bound_task(kernel, principal, base, point(10), pair,
                              [f'resource:{ids["grain"]}:bag'])) for pair in accounts]

    def seal(cut, label, actor_order):
        interaction = scheduler.prepare_interaction(principal, cut, point(10), [ids['sword']],
            {actor: grant['principal'].grant_id for actor, grant in grants.items()}, label)
        tasks = []
        for actor in actor_order:
            grant = grants[actor]
            task = create_bound_task(kernel, grant['principal'], cut, point(10), [actor],
                ['location'], plane='BELIEF', actor_id=actor)
            tasks.append((grant['principal'], task))
            with kernel.db.uow(write=False) as repo:
                context = repo.one('contexts', {'task_id': task['task_id'], 'generation': task['generation']})
            action = {'schema_version': '2.0', 'proposal_id': str(uuid4()), 'task_id': task['task_id'],
                'generation': task['generation'], 'context_bundle_id': context['context_bundle_id'],
                'world_ref': cut, 'actor_id': actor, 'intent': '按本人有限认知请求保管青霜剑。',
                'targets': [ids['sword']], 'story_interval': {'start': point(10), 'end': point(11), 'bounds': '[)'},
                'resource_requests': [], 'preconditions': [], 'evidence_refs': [],
                'dependency_fingerprint': context['dependency_fingerprint'],
                'idempotency_key': label + ':' + actor}
            assert scheduler.submit_action(grant['principal'], interaction['interaction_id'], action)
            assert scheduler.submit_action(grant['principal'], interaction['interaction_id'], action)
        selected = {'template_id': 'TRANSFER_ITEM', 'args': {'item_id': ids['sword'],
            'from_id': ids['smith'], 'to_id': actors[0], 'transfer_kind': 'GIFT', 'at': point(10)}}
        outcome_ref = scheduler.choose_outcome(principal, interaction['interaction_id'], commands=[selected],
            process_outcomes=[{'process_id': process_id, 'outcome': 'FIRED',
                              'world_stage': 'COMPLETED', 'next_due': None}],
            reasoning_summary='TEST_FIXTURE 固定接受 actors[0] 的保管请求；输入顺序不参与决定。',
            rule_evidence_refs=rule_refs, source_kind='FIXTURE_SELECTED')
        with kernel.db.uow(write=False) as repo:
            outcome = kernel.artifact(repo, outcome_ref, cut['work_id'])['payload']
            interaction_row = repo.one('simulation_interactions', {'interaction_id': interaction['interaction_id']})
            assert len(repo.many('simulation_intents', {'interaction_id': interaction['interaction_id']})) == 20
        args = outcome_command(interaction_row['artifact_ref'], interaction_row['payload'], outcome_ref, outcome)
        return interaction, tasks, selected, prepare_command(kernel, principal, cut, 'JOINT_INTERACTION', args)

    old_group, old_tasks, selected_before, old_joint = seal(base, 'original-group', list(reversed(actors)))
    population_ids = ([task['task_id'] for task in readers]
        + [command['task']['task_id'] for command in commands]
        + [task['task_id'] for _, task in old_tasks])
    assert len(set(population_ids)) == 200
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(lambda command: kernel.accept(principal, command['request']), commands))
    assert len({receipt['world_ref']['world_revision_id'] for receipt in receipts}) == 20
    assert all(receipt['original_base_world_ref'] == base for receipt in receipts)
    with pytest.raises(DomainError) as stale:
        kernel.accept(principal, old_joint['request'])
    assert stale.value.code == 'HEAD_CONFLICT'
    with kernel.db.uow(write=False) as repo:
        current = kernel.head(repo, base['work_id'])
        revision = kernel.revision(repo, current)
        assert len(revision['selection']) == 20
        assert state_at(revision['snapshot'], ids['sword'], 'active_holder', point(10))['data'] == entity_value(ids['smith'])
        for pair in accounts:
            assert resource_at(revision['snapshot'], pair[0], ids['grain'], 'bag', point(11))['quantity'] == '99'
    for actor_principal, task in old_tasks:
        kernel.cancel_task(actor_principal, task['task_id'], task['generation'], 'retire-stale:' + task['task_id'])
    new_group, _new_tasks, selected_after, joint = seal(current, 'requalified-group', actors)
    assert selected_before == selected_after
    accepted = kernel.accept(principal, joint['request'])
    assert kernel.accept(principal, joint['request']) == accepted
    assert old_group['interaction_id'] != new_group['interaction_id']
    with kernel.db.uow(write=False) as repo:
        final = kernel.revision(repo, accepted['world_ref'])
        assert len(final['selection']) == 21
        assert state_at(final['snapshot'], ids['sword'], 'active_holder', point(10))['data'] == entity_value(actors[0])
        assert kernel.revision(repo, base)['selection'] == {}
    query = {'schema_version': '2.1', 'task_id': readers[0]['task_id'], 'generation': readers[0]['generation'],
        'read_contract': {'revision': {'mode': 'FIXED', 'world_ref': base}, 'process_policy': 'ALLOW_PARTIAL',
                          'index_policy': 'AUTHORITATIVE', 'max_wait_ms': 0},
        'knowledge_cut': {'plane': 'WORLD'}, 'subject_time': {'kind': 'POINT', 'at': point(1)},
        'query': {'kind': 'STATE', 'subject_ids': [ids['sword']], 'predicates': ['active_holder']},
        'page_size': 10, 'cursor': None}
    historical = WorldQueryService(kernel).query(principal, query)
    assert historical['payload']['state_items'][0]['answer']['data'] == entity_value(ids['smith'])
    path = Path(os.environ.get('NF_P2_ACCEPTANCE_EVIDENCE_DIR', tmp_path))
    path.mkdir(parents=True, exist_ok=True)
    (path/'r1_f_200_population.json').write_text(json.dumps({
        'source': 'TEST_FIXTURE', 'base_world_ref': base, 'logical_task_ids': population_ids,
        'read_slots': 160, 'independent_commands': 20, 'sealed_intents': 20,
        'independent_receipts': receipts, 'stale_joint_error': stale.value.code,
        'old_interaction': old_group, 'new_interaction': new_group,
        'old_input_retired_explicitly': True, 'selected_content_sha256': fingerprint(selected_before),
        'requalified_joint_receipt': accepted, 'new_effects': 21,
        'limitation': 'Joint interaction remains STRICT_HEAD; unrelated prior commits require '
                      'explicit new common-cut actor inputs. No guarded joint optimization claimed.'},
        ensure_ascii=False, indent=2)+'\n')
