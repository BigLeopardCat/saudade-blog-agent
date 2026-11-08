"""Resolve checkpoint references through the authoritative summary-operation ledger."""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from pydantic import ValidationError

from agent.contracts.client import reference_adapter
from agent.domains import article_editor
from agent.domains.article_summary_state import (
    ApprovedSummary,
    AwaitingSummary,
    UnverifiedSummary,
    VerifiedSummary,
    projection,
    restore,
    saved_result,
)
from agent.runtime import memory, writes
from tools.result import ok
from utils.trace import record


class SummaryApprovalMiddleware(AgentMiddleware):
    def __init__(self, session):
        self.session = session

    def _load(self, store, reference):
        previous = store.lookup(reference['ledger_key'])
        if previous is None or previous[0] != 'done':
            raise ValueError('Client operation ledger is unavailable')
        return restore(previous[1], reference), previous[1]

    def _apply(self, store, reference):
        operation, previous = self._load(store, reference)
        if isinstance(operation, ApprovedSummary):
            store.replace(reference['ledger_key'], previous, saved_result(operation.awaiting()))
            operation, previous = self._load(store, reference)
        if not isinstance(operation, AwaitingSummary):
            return operation
        args = operation.args
        try:
            document = article_editor._run(self.session.config, 'set_article_summary', 'summary',
                args.request_id, editor_id=args.editor_id, revision=args.revision,
                expected_revision=operation.command()['expected_revision'],
                description=operation.description)
            observed = operation.verify(document)
        except ValidationError:
            observed = operation.uncertain('Client observation does not prove the approved summary')
        except ToolException as error:
            observed = operation.uncertain(str(error)[:1000] or 'Client observation is unavailable')
        store.replace(reference['ledger_key'], previous, saved_result(observed))
        operation, _ = self._load(store, reference)
        record('native', 'client_operation_observed', operation_id=operation.operation_id,
               status=operation.phase)
        return operation

    def before_model(self, state, runtime):
        references = state.get('client_operations') or {}
        if not references:
            return None
        principal, cid = writes.bound_context(self.session.config)
        store = writes.ledger_for(self.session.store.saver)
        messages, outcome = [], None
        for operation_id, raw in references.items():
            reference = reference_adapter.validate_python(raw, strict=True)
            if (reference['uid'] != principal.uid or reference['conv'] != cid
                    or reference['operation_id'] != operation_id):
                raise ValueError('Client operation reference differs from its conversation')
            if reference['tool'] != 'set_article_summary':
                continue
            operation = self._apply(store, reference)
            self.session.receipts = [row for row in self.session.receipts
                                    if row.get('operation_id') != operation_id]
            row = projection(operation)
            if isinstance(operation, VerifiedSummary):
                self.session.receipts.append(row)
            elif isinstance(operation, UnverifiedSummary):
                outcome = {**operation.spec(), 'status': 'unverified', 'result': row['result']}
            else:
                raise ValueError('Client operation did not reach an observed outcome')
            call_id = 'editor_result_' + operation_id
            if not any(isinstance(message, ToolMessage) and message.tool_call_id == call_id
                       for message in state.get('messages', ())):
                observed = memory.observation({'name': 'set_article_summary', 'args': row['args'],
                    'id': call_id}, ok(json.dumps(row, ensure_ascii=False)))
                for index, message in enumerate(observed):
                    message.id = call_id + '_' + str(index)
                messages.extend(observed)
        effects = {**(state.get('effects') or {})}
        if outcome:
            effects['operation_outcome'] = outcome
        self.session.state['effects'] = effects
        return {'effects': effects, 'messages': messages}
