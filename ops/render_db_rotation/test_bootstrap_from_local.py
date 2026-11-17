import pytest

from ops.render_db_rotation.bootstrap_from_local import (
    BootstrapError,
    build_create_settings,
)


def test_build_create_settings_copies_safe_database_configuration():
    result = build_create_settings(
        {
            "name": "learn-log",
            "databaseName": "learnlog",
            "databaseUser": "learnlog_user",
            "owner": {"id": "tea-123"},
            "plan": "free",
            "region": "oregon",
            "version": "18",
            "environmentId": "evm-123",
            "ipAllowList": [{"cidrBlock": "0.0.0.0/0", "description": "everywhere"}],
        }
    )

    assert result == {
        "name": "learn-log",
        "databaseName": "learnlog",
        "databaseUser": "learnlog_user",
        "ownerId": "tea-123",
        "plan": "free",
        "region": "oregon",
        "version": "18",
        "environmentId": "evm-123",
        "ipAllowList": [{"cidrBlock": "0.0.0.0/0", "description": "everywhere"}],
    }


def test_build_create_settings_rejects_missing_owner():
    with pytest.raises(BootstrapError, match="ownerId"):
        build_create_settings(
            {
                "name": "learn-log",
                "databaseName": "learnlog",
                "databaseUser": "learnlog_user",
                "region": "oregon",
                "version": "18",
            }
        )
