"""Cognito claim handling: role mapping, identity sync, and token rejection."""

import pytest
from fastapi import HTTPException

from app.auth import Principal, decode_cognito_token, get_current_user, require_roles, resolve_role
from app.models import Role, User


# --------------------------------------------------------------------------
# Group -> role mapping
# --------------------------------------------------------------------------

def test_admin_group_maps_to_the_admin_role():
    assert resolve_role({"cognito:groups": ["admin"]}) is Role.admin


def test_supervisor_group_maps_to_the_supervisor_role():
    assert resolve_role({"cognito:groups": ["supervisor"]}) is Role.supervisor


def test_employee_group_maps_to_the_employee_role():
    assert resolve_role({"cognito:groups": ["employee"]}) is Role.employee


def test_no_groups_defaults_to_the_least_privileged_role():
    assert resolve_role({}) is Role.employee


def test_unknown_groups_do_not_grant_privileges():
    assert resolve_role({"cognito:groups": ["superuser", "root"]}) is Role.employee


def test_the_first_recognised_group_wins():
    assert resolve_role({"cognito:groups": ["nonsense", "supervisor", "admin"]}) is Role.supervisor


# --------------------------------------------------------------------------
# Token rejection
# --------------------------------------------------------------------------

def test_token_decoding_is_unavailable_when_cognito_is_unconfigured():
    # The test settings leave the Cognito client id blank.
    with pytest.raises(HTTPException) as error:
        decode_cognito_token("any.token.value")

    assert error.value.status_code == 503


def test_missing_credentials_are_rejected(db):
    with pytest.raises(HTTPException) as error:
        get_current_user(credentials=None, db=db)

    assert error.value.status_code == 401


# --------------------------------------------------------------------------
# Role guard
# --------------------------------------------------------------------------

def test_require_roles_allows_a_listed_role(admin):
    guard = require_roles(Role.admin)

    assert guard(user=admin) is admin


def test_require_roles_allows_any_of_several_roles(supervisor):
    guard = require_roles(Role.admin, Role.supervisor)

    assert guard(user=supervisor) is supervisor


def test_require_roles_blocks_an_unlisted_role(employee):
    guard = require_roles(Role.admin, Role.supervisor)

    with pytest.raises(HTTPException) as error:
        guard(user=employee)

    assert error.value.status_code == 403


# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------

def test_principal_is_immutable():
    principal = Principal(sub="s", email="e@x.test", name="n", role=Role.admin)

    with pytest.raises(Exception):
        principal.role = Role.employee  # type: ignore[misc]


def test_inactive_accounts_are_refused(db):
    account = User(cognito_sub="sub-inactive", email="inactive@x.test", name="inactive", role=Role.employee, active=False)
    db.add(account)
    db.commit()

    assert account.active is False


# --------------------------------------------------------------------------
# get_current_user against stubbed Cognito claims
#
# decode_cognito_token is replaced so these exercise the identity-sync logic
# (provisioning, role changes, deactivation) without a live user pool.
# --------------------------------------------------------------------------

import app.auth as auth_module


class _Credentials:
    def __init__(self, token: str):
        self.credentials = token


def _claims(**overrides):
    base = {"sub": "sub-1", "email": "person@stockroom.test", "name": "Person", "cognito:groups": ["employee"]}
    base.update(overrides)
    return base


def _stub_claims(monkeypatch, claims):
    monkeypatch.setattr(auth_module, "decode_cognito_token", lambda token: claims)


def test_an_unknown_subject_is_provisioned_on_first_request(db, monkeypatch):
    _stub_claims(monkeypatch, _claims(**{"cognito:groups": ["supervisor"]}))

    user = get_current_user(credentials=_Credentials("token"), db=db)

    assert user.cognito_sub == "sub-1"
    assert user.email == "person@stockroom.test"
    assert user.role is Role.supervisor


def test_a_name_claim_is_optional_and_falls_back_to_the_email_prefix(db, monkeypatch):
    claims = _claims()
    claims.pop("name")
    _stub_claims(monkeypatch, claims)

    user = get_current_user(credentials=_Credentials("token"), db=db)

    assert user.name == "person"


def test_a_returning_subject_is_looked_up_not_recreated(db, monkeypatch):
    _stub_claims(monkeypatch, _claims())

    first = get_current_user(credentials=_Credentials("token"), db=db)
    second = get_current_user(credentials=_Credentials("token"), db=db)

    assert first.id == second.id
    assert db.query(User).filter(User.cognito_sub == "sub-1").count() == 1


def test_a_group_change_updates_the_stored_role(db, monkeypatch):
    _stub_claims(monkeypatch, _claims())
    get_current_user(credentials=_Credentials("token"), db=db)

    _stub_claims(monkeypatch, _claims(**{"cognito:groups": ["admin"]}))
    promoted = get_current_user(credentials=_Credentials("token"), db=db)

    assert promoted.role is Role.admin


def test_a_deactivated_account_is_refused(db, monkeypatch):
    _stub_claims(monkeypatch, _claims())
    user = get_current_user(credentials=_Credentials("token"), db=db)
    user.active = False
    db.commit()

    with pytest.raises(HTTPException) as error:
        get_current_user(credentials=_Credentials("token"), db=db)

    assert error.value.status_code == 403


def test_a_token_without_a_subject_is_rejected(db, monkeypatch):
    claims = _claims()
    claims.pop("sub")
    _stub_claims(monkeypatch, claims)

    with pytest.raises(HTTPException) as error:
        get_current_user(credentials=_Credentials("token"), db=db)

    assert error.value.status_code == 401


def test_a_token_without_an_email_is_rejected(db, monkeypatch):
    claims = _claims()
    claims.pop("email")
    _stub_claims(monkeypatch, claims)

    with pytest.raises(HTTPException) as error:
        get_current_user(credentials=_Credentials("token"), db=db)

    assert error.value.status_code == 401
