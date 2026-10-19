import pytest

from smartgpms.auth import AuthManager, hash_password, verify_password
from smartgpms.database import Database


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("StrongPass123")
    second = hash_password("StrongPass123")

    assert first != second
    assert verify_password("StrongPass123", first)
    assert not verify_password("wrong-password", first)


def test_first_user_is_protected_admin_and_username_is_case_insensitive(tmp_path):
    manager = AuthManager(Database(tmp_path / "smartgpms.sqlite3"))

    admin = manager.setup_admin("Admin.User", "StrongPass123", "系统管理员")

    assert manager.setup_required() is False
    assert admin["role"] == "admin"
    assert admin["is_protected"] is True
    assert manager.authenticate("admin.user", "StrongPass123")["id"] == admin["id"]
    with pytest.raises(ValueError, match="已经存在"):
        manager.create_user("ADMIN.USER", "AnotherPass123", "重复", "operator")


def test_session_resolves_and_password_reset_revokes_it(tmp_path):
    manager = AuthManager(Database(tmp_path / "smartgpms.sqlite3"))
    admin = manager.setup_admin("admin", "StrongPass123")
    token = manager.create_session(admin["id"])

    assert manager.resolve_session(token)["username"] == "admin"

    manager.reset_password(admin["id"], "ChangedPass123")

    assert manager.resolve_session(token) is None
    assert manager.authenticate("admin", "StrongPass123") is None
    assert manager.authenticate("admin", "ChangedPass123") is not None


def test_protected_admin_cannot_be_disabled_or_demoted(tmp_path):
    database = Database(tmp_path / "smartgpms.sqlite3")
    manager = AuthManager(database)
    admin = manager.setup_admin("admin", "StrongPass123")

    with pytest.raises(ValueError, match="不能停用"):
        database.update_app_user(
            admin["id"], display_name="管理员", role="admin", is_active=False
        )
    with pytest.raises(ValueError, match="不能停用"):
        database.update_app_user(
            admin["id"], display_name="管理员", role="operator", is_active=True
        )


def test_disabled_user_cannot_authenticate_and_existing_session_is_revoked(tmp_path):
    database = Database(tmp_path / "smartgpms.sqlite3")
    manager = AuthManager(database)
    manager.setup_admin("admin", "StrongPass123")
    operator = manager.create_user(
        "operator.one", "OperatorPass123", "操作员", "operator"
    )
    token = manager.create_session(operator["id"])

    database.update_app_user(
        operator["id"], display_name="操作员", role="operator", is_active=False
    )

    assert manager.resolve_session(token) is None
    assert manager.authenticate("operator.one", "OperatorPass123") is None
