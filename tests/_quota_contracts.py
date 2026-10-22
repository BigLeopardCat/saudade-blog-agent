"""跨语言额度审批事务契约。"""


def check_review_transaction(check, routes: str, service: str) -> None:
    if service:
        check("旧额度接口委托共用事务服务，避免独立的审批写通道",
              "super::quota_actions::service::review(" in routes
              and "super::quota_actions::service::reset(" in routes)
        _c0 = service.find("let result = quota_request::Entity::update_many()")
        _cseg = service[_c0:service.find(".exec(txn)", _c0)]
        check("审核UPDATE仍限定具体申请、账号与待处理状态",
              "quota_request::Column::Status.eq(quota::STATUS_PENDING)" in _cseg
              and "quota_request::Column::Id.eq(id)" in _cseg
              and "quota_request::Column::UserId.eq(target.id)" in _cseg)
        _i_check = service.find("if result.rows_affected != 1", _c0)
        _i_zero = service.find(
            'return Err("这条申请已经处理过了".into())', _i_check)
        _i_wipe = service.find("user::Column::ChatQuotaUsed, Expr::value(0)", _c0)
        check("认领失败先返回，额度清零始终在成功认领之后",
              0 <= _c0 < _i_check < _i_zero < _i_wipe,
              f"认领={_i_zero} 清零={_i_wipe}")
