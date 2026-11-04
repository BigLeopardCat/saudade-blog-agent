"""Summary lifecycle records; the execution ledger owns phase and content evidence."""

import hashlib
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)

from agent.contracts.client import ClientOperationRef, EditorObservation, reference_adapter
from agent.runtime import writes
from tools.result import ToolResult, ok

Digest = Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')]
OperationId = Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{32}$')]
Positive = Annotated[StrictInt, Field(gt=0)]


class SummaryArguments(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    editor_id: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    revision: Annotated[StrictInt, Field(ge=0, le=2**53 - 1)]
    summary_sha256: Digest
    previous_summary_sha256: Digest
    request_id: Annotated[StrictStr, Field(min_length=1, max_length=100)]


class SummaryIntent(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    operation_id: OperationId
    uid: Positive
    conv: Positive
    args: SummaryArguments
    description: Annotated[StrictStr, Field(max_length=16000)]

    @model_validator(mode='after')
    def content_is_approved(self):
        if hashlib.sha256(self.description.encode()).hexdigest() != self.args.summary_sha256:
            raise ValueError('Summary content differs from its approved digest')
        return self

    def spec(self):
        return {'tool': 'set_article_summary', 'args': self.args.model_dump()}

    def reference(self) -> ClientOperationRef:
        grant = {'uid': self.uid, 'conv': self.conv, 'jti': self.operation_id}
        return reference_adapter.validate_python({'uid': self.uid, 'conv': self.conv,
            'operation_id': self.operation_id,
            'tool': 'set_article_summary', 'ledger_key': writes.operation_key(
                grant, 'set_article_summary', self.args.model_dump())}, strict=True)

    def command(self):
        return {'kind': 'article_editor', 'operation': 'summary',
                'request_id': self.args.request_id, 'editor_id': self.args.editor_id,
                'revision': self.args.revision,
                'expected_revision': self.args.revision + (
                    self.args.summary_sha256 != self.args.previous_summary_sha256),
                'description': self.description}


class ApprovedSummary(SummaryIntent):
    phase: Literal['approved'] = 'approved'

    def awaiting(self):
        return AwaitingSummary.model_validate({**self.model_dump(), 'phase': 'awaiting_client'})


class AwaitingSummary(SummaryIntent):
    phase: Literal['awaiting_client'] = 'awaiting_client'

    def verify(self, document):
        return VerifiedSummary.model_validate({**self.model_dump(), 'phase': 'verified',
                                               'document': document})

    def uncertain(self, reason):
        return UnverifiedSummary.model_validate({**self.model_dump(), 'phase': 'unverified',
                                                 'reason': reason})


class VerifiedSummary(SummaryIntent):
    phase: Literal['verified'] = 'verified'
    document: EditorObservation

    @model_validator(mode='after')
    def matching_observation(self):
        if not self.document.matches(self.command()):
            raise ValueError('Summary observation does not prove the approved content')
        return self


class UnverifiedSummary(SummaryIntent):
    phase: Literal['unverified'] = 'unverified'
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]


SummaryOperation = Annotated[
    ApprovedSummary | AwaitingSummary | VerifiedSummary | UnverifiedSummary,
    Field(discriminator='phase')]
operation_adapter: TypeAdapter[SummaryOperation] = TypeAdapter(SummaryOperation)


def projection(operation):
    args = operation.args
    visible = {'editor_id': args.editor_id, 'revision': args.revision,
               'summary_sha256': args.summary_sha256}
    if isinstance(operation, VerifiedSummary):
        text = ('编辑草稿摘要已符合确认内容，无需再次修改；未发布。'
                if operation.document.outcome == 'unchanged' else
                '摘要已更新到编辑草稿，未发布；服务端保存以自动保存状态为准。')
        return {'tool': 'set_article_summary', 'operation_id': operation.operation_id,
                'status': 'completed', 'scope': 'editor_draft', 'published': False,
                'args': visible, 'result': text,
                'document': {'scope': 'editor_draft', 'published': False,
                             **operation.document.model_dump(exclude_none=True)}}
    if isinstance(operation, UnverifiedSummary):
        return {'tool': 'set_article_summary', 'args': visible, 'status': 'unverified',
                'result': '未能确认摘要应用结果：' + operation.reason}
    return {'tool': 'set_article_summary', 'operation_id': operation.operation_id,
            'status': 'approved', 'args': args.model_dump(),
            'result': '摘要修改已获得确认，应用结果以编辑器回执为准。'}


def saved_result(operation):
    return ok(projection(operation)['result'], meta={
        'client_operation': operation.model_dump(mode='json')})


def restore(row, reference):
    from agent import idem

    value = idem.replay_result(row)
    if not isinstance(value, ToolResult) or value.kind != 'ok':
        raise ValueError('Client operation is unavailable or revoked')
    operation = operation_adapter.validate_python(value.meta.get('client_operation'))
    if operation.reference() != reference:
        raise ValueError('Client operation reference does not match its ledger record')
    return operation
