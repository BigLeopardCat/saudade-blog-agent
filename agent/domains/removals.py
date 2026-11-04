"""Versioned deletion proposals over service-owned snapshots and the shared execution ledger."""

import time
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent import authz
from agent.proposals import proposal
from agent.reports import sanitize_untrusted
from agent.runtime import writes
from agent.runtime.results import TurnResult
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import invalid_args

Identifier = Annotated[StrictInt, Field(gt=0, le=2147483647)]
Identifiers = Annotated[list[Identifier], Field(max_length=1000)]
Kind = Literal['all', 'notice', 'announcement']


class Item(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Identifier
    title: StrictStr


class Plan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')]
    ids: Identifiers
    items: Annotated[list[Item], Field(max_length=20)]
    count: Annotated[StrictInt, Field(ge=0, le=1000)]
    associatedNotifications: Annotated[StrictInt, Field(ge=0)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ids: Annotated[list[Identifier], Field(min_length=1, max_length=1000)]
    version: Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')]
    kind: Kind
    associated_notifications: Annotated[StrictInt, Field(ge=0)]


@dataclass(frozen=True)
class Removal:
    executor: str
    operation: str
    path: str
    label: str
    admin: bool = False

    @property
    def workflow(self):
        return self.executor

    def available(self, config):
        try:
            principal, _ = writes.bound_context(config)
            return authz.check(principal, self.operation).allowed
        except ValueError:
            return False

    def handles(self, grant):
        return (isinstance(grant, dict) and grant.get('executor') == self.executor
                and grant.get('workflow') == self.workflow)

    def prepare(self, scope, ids, kind, config):
        if not self.available(config):
            return base.unavailable('当前身份或会话不可用，本次未生成删除确认卡。')
        if (scope == 'all' and ids or scope == 'selected' and not ids
                or len(ids) != len(set(ids)) or self.admin and kind != 'all'):
            return invalid_args('请选择明确编号或全部范围；两种范围不能混用。')
        check_cancelled(config)
        read = base._admin_read_post if self.admin else base._own_read_post
        value = read(self.path + '/prepare', {'scope': scope, 'ids': ids, 'kind': kind}, config)
        if isinstance(value, base.ToolResult):
            return value
        try:
            plan = Plan.model_validate(value)
            if (plan.ids != sorted(set(plan.ids)) or plan.count != len(plan.ids)
                    or [row.id for row in plan.items] != plan.ids[:20]
                    or scope == 'selected' and plan.ids != sorted(ids)
                    or not self.admin and plan.associatedNotifications != 0):
                raise ValueError('incomplete deletion snapshot')
        except (ValueError, TypeError):
            return base.unavailable('删除范围无法完整核对，本次未生成确认卡。')
        observed(self.executor, plan.model_dump(), ('count', 'items', 'associatedNotifications'))
        if not plan.ids:
            text = f'当前没有可删除的{self.label}。'
            return TurnResult(text, {'noop_note': text, 'noop_text': text, 'receipts': []})
        target = f'删除本次选定的 {plan.count} 条{self.label}'
        preview = '\n'.join(
            f'- #{row.id}《{sanitize_untrusted(row.title, 80)}》' for row in plan.items)
        impact = (f'将同时删除 {plan.associatedNotifications} 条关联个人通知，影响所有接收者。'
                  if self.admin else '仅从本人的通知列表移除，保留全站公告和其他人的通知。')
        more = (f'\n另有 {plan.count - len(plan.items)} 条纳入本次范围。'
                if plan.count > len(plan.items) else '')
        question = (f'{target}\n\n{preview}{more}\n\n{impact}\n'
                    '只处理本次固定编号；之后新增的条目不在范围内。确认删除？')
        principal, cid = writes.bound_context(config)
        return proposal(principal, cid, self.workflow, {'tool': self.operation, 'args': {
            'ids': plan.ids, 'version': plan.version, 'kind': kind,
            'associated_notifications': plan.associatedNotifications}},
            executor=self.executor, question=question, target=target)

    def validate(self, grant, config):
        principal = writes.validate_binding(grant, config)
        specs = grant.get('specs')
        if (not self.handles(grant) or not isinstance(specs, list) or len(specs) != 1
                or not isinstance(specs[0], dict) or specs[0].get('tool') != self.operation
                or not authz.check(principal, self.operation).allowed):
            raise ValueError('invalid deletion grant')
        args = Arguments.model_validate(specs[0].get('args')).model_dump()
        if (args['ids'] != sorted(set(args['ids'])) or self.admin and args['kind'] != 'all'
                or not self.admin and args['associated_notifications'] != 0):
            raise ValueError('invalid deletion scope')
        return self.operation, args

    def invoke(self, args, config=None):
        check_cancelled(config)
        request = base._admin_request if self.admin else base._own_request
        payload = {key: args[key] for key in ('ids', 'version', 'kind')}
        result = request('POST', self.path, payload, config)
        if isinstance(result, base.ToolResult):
            return result
        expected = {'version': args['version'], 'deletedIds': args['ids'],
                    'associatedNotifications': args['associated_notifications']}
        if (result != expected
                or any(type(value) is not int for value in result.get('deletedIds', []))
                or type(result.get('associatedNotifications')) is not int):
            return base.unavailable(
                '删除请求已发出，但回执无法完整核对；结果未知，请先查询，不要重复执行。')
        text = f'已删除本次确认的 {len(args["ids"])} 条{self.label}。'
        if self.admin:
            text += f'已处理 {args["associated_notifications"]} 条关联个人通知。'
        return base.ok(text, meta={'confirmed_args': args, 'deletion_result': expected})

    def receipt(self, grant, name, args, result):
        meta = getattr(result, 'meta', {}) or {}
        expected = {'version': args['version'], 'deletedIds': args['ids'],
                    'associatedNotifications': args['associated_notifications']}
        if (not isinstance(result, base.ToolResult) or result.kind != 'ok'
                or meta.get('confirmed_args') != args or meta.get('deletion_result') != expected):
            return None
        return {'skill': self.workflow, 'tool': name, 'args': args,
                'result': str(result), 'action': str(result), 'ts': time.time()}

    def execute(self, grant, config, saver, *, tools=None, ledger=None):
        return writes.execute_confirmed(grant, config, saver, validate=self.validate,
            receipt=self.receipt,
            tool_factory=lambda name: {name: self} if tools is None else tools,
            ledger=ledger)
