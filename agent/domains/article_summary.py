"""Authorize a summary intent; the ledger owns its typed client lifecycle."""

import hashlib

from agent import authz
from agent.domains.article_summary_state import (
    ApprovedSummary,
    AwaitingSummary,
    SummaryArguments,
    UnverifiedSummary,
    VerifiedSummary,
    projection,
    restore,
    saved_result,
)
from agent.runtime import writes
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.turns import check_cancelled
from utils.trace import record

PERMISSION_OPERATIONS = frozenset({'set_article_summary'})


def handles(grant):
    return isinstance(grant, dict) and grant.get('workflow') == 'editor_summary'


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get('specs')
    if (not handles(grant) or grant.get('executor') != 'editor_summary'
            or not isinstance(specs, list) or len(specs) != 1
            or not isinstance(specs[0], dict) or specs[0].get('tool') != 'set_article_summary'):
        raise ValueError('确认内容与摘要修改不一致')
    args = SummaryArguments.model_validate(specs[0].get('args')).model_dump()
    if not authz.check(principal, 'set_article_summary').allowed:
        raise ValueError('当前身份无权修改文章摘要')
    return 'set_article_summary', args


def description_matches(description, args):
    return (isinstance(description, str) and len(description) <= 16000
            and hashlib.sha256(description.encode()).hexdigest() == args['summary_sha256'])


def execute(grant, config, saver, *, ledger=None):
    name, args = validate(grant, config)
    store = writes.ledger_for(saver) if ledger is None else ledger
    key = writes.operation_key(grant, name, args)
    reference = {'tool': name, 'operation_id': grant['jti'], 'uid': grant['uid'],
                 'conv': grant['conv'], 'ledger_key': key}
    previous = store.lookup(key)
    replay_only = (config.get('configurable') or {}).get('confirmation_replay_only')
    if previous is None and not replay_only:
        pending = CheckpointConversationStore(saver).load(config).pending_action or {}
        description = (pending.get('payload') or {}).get('description')
        if pending.get('jti') != grant['jti'] or not description_matches(description, args):
            return {'reply': '待确认摘要已变化或丢失，请重新准备；本次没有更新。',
                    'receipts': [], 'blocked': True}
        operation = ApprovedSummary(operation_id=grant['jti'], uid=grant['uid'], conv=grant['conv'],
                                    args=SummaryArguments.model_validate(args),
                                    description=description)
        check_cancelled(config)
        previous = store.claim(key, name, grant['jti'], resuming=True)
        if previous[0] == 'new':
            store.finish(key, saved_result(operation))
            previous = store.lookup(key)
            record('native', 'client_operation_approved', name=name, operation_id=grant['jti'])
    if previous is None or previous[0] != 'done':
        return {'reply': '这次摘要确认已失效或结果未知，请重新读取草稿后准备。', 'receipts': [],
                'blocked': True}
    try:
        operation = restore(previous[1], reference)
        if operation.spec() != {'tool': name, 'args': args}:
            raise ValueError('Summary operation differs from its approval')
    except ValueError:
        return {'reply': '这次摘要确认已撤销或无法核实，没有再次应用。',
                'receipts': [], 'blocked': True}
    projected = projection(operation)
    execution = {'reply': projected['result'], 'receipts': []}
    if isinstance(operation, VerifiedSummary):
        execution['receipts'] = [projected]
    elif isinstance(operation, UnverifiedSummary):
        execution['operation_outcome'] = {**operation.spec(), 'status': 'unverified',
                                          'result': projected['result']}
    elif isinstance(operation, (ApprovedSummary, AwaitingSummary)):
        execution['authorized_operation'] = projected
        if not replay_only:
            execution['client_operations'] = {operation.operation_id: reference}
    return execution
