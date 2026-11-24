from ops.render_db_rotation.postgres_ops import build_create_settings


def test_build_create_settings_copies_safe_database_fields():
    settings = build_create_settings(
        {
            "name": "learn-log",
            "databaseName": "learn_log",
            "databaseUser": "learn_log_user",
            "owner": {"id": "tea-workspace"},
            "plan": "free",
            "region": "singapore",
            "version": "18",
            "environmentId": "evm-1",
            "ipAllowList": [{"cidrBlock": "0.0.0.0/0", "description": "all"}],
        }
    )

    assert settings == {
        "name": "learn-log",
        "databaseName": "learn_log",
        "databaseUser": "learn_log_user",
        "ownerId": "tea-workspace",
        "plan": "free",
        "region": "singapore",
        "version": "18",
        "environmentId": "evm-1",
        "ipAllowList": [{"cidrBlock": "0.0.0.0/0", "description": "all"}],
    }
