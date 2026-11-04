"""Fixed deletion sets preserve recipient scope and replay safety across approvals."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.announcements import deletion as announcement
from agent.domains.notifications import deletion as notification
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.capabilities import ToolCatalog
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from tools import base


def context(admin=False):
    return config(principal=Principal(uid=7, role='admin' if admin else 'user',
                                      source=SOURCE_ASSERTION))


@pytest.fixture(params=[notification, announcement], ids=['personal', 'global'])
def domain(request):
    return request.param


def prepare(domain, **args):
    tool = (domain.prepare_announcement_deletions if domain is announcement
            else domain.prepare_notification_deletions)
    return tool.invoke({'scope': 'all', **args}, config=context(domain is announcement))


def signed(prepared):
    return confirm.verify(prepared.execution['pending_confirm']['token'], 7, 42)


@pytest.fixture
def service(monkeypatch):
    state = {'rows': {11: '第一条', 12: '第二条'}, 'requests': [], 'writes': 0}

    def snapshot(ids, admin):
        return {'version': hashlib.sha256(json.dumps(
            [ids, [state['rows'][i] for i in ids], admin]).encode()).hexdigest(),
            'ids': ids, 'items': [{'id': i, 'title': state['rows'][i]} for i in ids[:20]],
            'count': len(ids), 'associatedNotifications': len(ids) if admin else 0}

    def handle(request):
        data = json.loads(request.content)
        state['requests'].append((request.url.path, data))
        admin = '/announcement-deletions' in request.url.path
        ids = sorted(state['rows']) if data.get('scope') == 'all' else sorted(data['ids'])
        if any(i not in state['rows'] for i in ids):
            return httpx.Response(200, json={'code': 409, 'message': 'missing target'})
        plan = snapshot(ids, admin)
        if request.url.path.endswith('/prepare'):
            if state.get('bad_plan'):
                plan.update(state['bad_plan'])
            return httpx.Response(200, json={'code': 200, 'data': plan})
        if data['version'] != plan['version']:
            return httpx.Response(200, json={'code': 409, 'message': 'changed target'})
        state['writes'] += 1
        for i in ids:
            del state['rows'][i]
        if state.get('lost'):
            raise httpx.ReadTimeout('committed, response lost')
        result = {'version': plan['version'], 'deletedIds': ids,
                  'associatedNotifications': plan['associatedNotifications']}
        result.update(state.get('bad_receipt', {}))
        return httpx.Response(200, json={'code': 200, 'data': result})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, '_client', client)
        monkeypatch.setattr(confirm.settings, 'jwt_secret', secrets.token_urlsafe(32))
        yield state


def test_one_approval_and_request_preserve_frozen_set_on_replay(domain, service, tmp_path):
    prepared = prepare(domain)
    grant = signed(prepared)
    assert grant['specs'][0]['args']['ids'] == [11, 12]
    assert len(service['requests']) == 1 and service['writes'] == 0
    assert prepared.reply.count('#11') == 1
    assert ('所有接收者' if domain is announcement else '保留全站公告') in prepared.reply
    service['rows'][13] = '确认之后到达'
    ledger = idem.Ledger(str(tmp_path / 'once.sqlite3'))
    for _ in range(2):
        result = domain.execute(grant, context(domain is announcement), None, ledger=ledger)
        assert len(result['receipts']) == 1 and not result['blocked']
    assert service['writes'] == 1 and service['rows'] == {13: '确认之后到达'}
    payload = service['requests'][-1][1]
    assert payload['ids'] == [11, 12] and 'scope' not in payload
    assert set(payload) == {'ids', 'version', 'kind'}


def test_changed_target_rejects_whole_batch(domain, service, tmp_path):
    grant = signed(prepare(domain))
    service['rows'][12] = '内容已变化'
    result = domain.execute(grant, context(domain is announcement), None,
                            ledger=idem.Ledger(str(tmp_path / 'changed.sqlite3')))
    assert not result['receipts'] and not service['writes']
    assert set(service['rows']) == {11, 12}


@pytest.mark.parametrize('fault', ['lost', 'wrong_ids', 'wrong_impact', 'boolean_impact'])
def test_uncertain_results_never_reexecute_or_claim_success(domain, service, tmp_path, fault):
    grant = signed(prepare(domain))
    if fault == 'lost':
        service['lost'] = True
    else:
        service['bad_receipt'] = {
            'wrong_ids': {'deletedIds': [11]},
            'wrong_impact': {'associatedNotifications': 99},
            'boolean_impact': {'associatedNotifications': False},
        }[fault]
    ledger = idem.Ledger(str(tmp_path / 'unknown.sqlite3'))
    for _ in range(2):
        assert not domain.execute(grant, context(domain is announcement), None,
                                  ledger=ledger)['receipts']
    assert service['writes'] == 1


def test_empty_list_does_not_offer_a_deletion(domain, service):
    service['rows'].clear()
    result = prepare(domain)
    assert result.execution['noop_note'] and not result.execution.get('pending_confirm')
    assert not service['writes']


@pytest.mark.parametrize('plan', [
    {'count': 1}, {'ids': [12, 11]}, {'items': []},
    {'items': [{'id': 99, 'title': 'unrequested'}]}, {'version': 'not-a-version'},
])
def test_incomplete_or_misordered_snapshot_cannot_be_confirmed(domain, service, plan):
    service['bad_plan'] = plan
    assert prepare(domain).kind == 'unavailable' and not service['writes']


@pytest.mark.parametrize('args', [
    {'scope': 'all', 'ids': [11]}, {'scope': 'selected', 'ids': []},
    {'scope': 'selected', 'ids': [11, 11]},
])
def test_invalid_scope_mixes_fail_before_network(domain, service, args):
    assert prepare(domain, **args).kind == 'invalid_args' and not service['requests']


@pytest.mark.parametrize('ids', [[True], [0], [-1], [2147483648], list(range(1, 1002))])
def test_invalid_identifiers_never_reach_service(domain, service, ids):
    with pytest.raises(ValidationError):
        prepare(domain, scope='selected', ids=ids)
    assert not service['requests']


def test_large_approval_is_bounded_and_fits_signed_transport(domain, service):
    service['rows'] = {2147483647 - i: '标题' * 80 for i in range(1000)}
    result = prepare(domain)
    token = result.execution['pending_confirm']['token']
    assert len(token) <= confirm.MAX_TOKEN_CHARS
    assert len(signed(result)['specs'][0]['args']['ids']) == 1000
    assert '另有 980 条' in result.reply and result.reply.count('《') == 20


def test_revocation_or_identity_change_never_reaches_write(domain, service, tmp_path, monkeypatch):
    result = prepare(domain)
    grant = signed(result)
    ledger = idem.Ledger(str(tmp_path / 'cancel.sqlite3'))
    monkeypatch.setattr(writes, 'ledger_for', lambda saver: ledger)
    pending = PendingOperation(grant, result.execution['pending_action'])
    pending.cancel(context(domain is announcement), None)
    assert not domain.execute(grant, context(domain is announcement), None,
                              ledger=ledger)['receipts']
    for changes in ({'uid': 8}, {'conv': 43}, {'executor': 'different'}, {'specs': [None]}):
        altered = {**copy.deepcopy(grant), **changes}
        assert not domain.execute(altered, context(domain is announcement), None,
                                  ledger=ledger)['receipts']
    assert not service['writes']


def test_global_permission_cannot_be_borrowed_from_personal_deletion(service, tmp_path):
    grant = signed(prepare(announcement))
    assert announcement.prepare_announcement_deletions.invoke(
        {'scope': 'all'}, config=context()).kind == 'unavailable'
    assert not announcement.execute(grant, context(), None,
        ledger=idem.Ledger(str(tmp_path / 'role.sqlite3')))['receipts']
    assert not service['writes']


def test_authorized_menus_keep_deletion_distinct_from_read_and_global_scope():
    catalog = ToolCatalog()
    personal = catalog.domains(context(), True)
    admin = catalog.domains(context(True), True)
    assert 'prepare_notification_deletions' in personal['notifications']
    assert 'prepare_notification_read' in personal['notifications']
    assert 'admin_announcements' not in personal
    assert {'prepare_announcement_create', 'prepare_announcement_update',
            'prepare_announcement_delete', 'prepare_announcement_deletions'} <= set(
                admin['admin_announcements'])


@pytest.mark.parametrize('copy_type,expected', [('all', 'all'),
    ('personal_notice_copies', 'notice'), ('announcement_copies', 'announcement')])
def test_notification_copy_filter_preserves_the_backend_scope(service, copy_type, expected):
    result = prepare(notification, notification_type=copy_type)
    assert signed(result)['specs'][0]['args']['kind'] == expected
    assert service['requests'][0][1]['kind'] == expected
    assert '/notification-deletions/prepare' in service['requests'][0][0]
    assert service['writes'] == 0


def test_unknown_or_obsolete_copy_filter_cannot_silently_expand_scope(service):
    for args in ({'notification_type': 'notice'}, {'kind': 'notice'}):
        with pytest.raises(ValidationError):
            prepare(notification, **args)
    assert not service['requests']
