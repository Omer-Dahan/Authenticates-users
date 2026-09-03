import pytest
from sqlalchemy import select
from database.init_db import DEFAULT_GROUP_CONFIG, seed_new_group
from database.models import GroupConfig, Group
from database.clone_utils import clone_group_settings
from bot.handlers.join_requests import _get_config_value


def test_default_group_config_includes_notify_admin_setting():
    setting = next((c for c in DEFAULT_GROUP_CONFIG if c["key"] == "notify_admin_on_manual_review"), None)
    assert setting is not None
    assert setting["value"] == "false"
    assert setting["value_type"] == "bool"


@pytest.mark.asyncio
async def test_seed_new_group_creates_notify_setting(test_session_factory, sample_group, db_session):
    await seed_new_group(sample_group.id, db_session)
    await db_session.commit()

    async with test_session_factory() as session:
        res = await session.execute(
            select(GroupConfig).where(
                GroupConfig.group_id == sample_group.id,
                GroupConfig.key == "notify_admin_on_manual_review",
            )
        )
        cfg = res.scalar_one_or_none()
        assert cfg is not None
        assert cfg.value == "false"
        assert cfg.value_type == "bool"

        # Verify _get_config_value returns False
        parsed_val = await _get_config_value(sample_group.id, "notify_admin_on_manual_review", None, session)
        assert parsed_val is False


@pytest.mark.asyncio
async def test_clone_settings_copies_notify_setting(test_session_factory, sample_group, db_session):
    # Set custom value in source group
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="notify_admin_on_manual_review",
        value="true",
        value_type="bool",
    ))
    # Target group
    target_group = Group(
        chat_id=-1005554443332,
        title="Target Group",
        owner_id=sample_group.owner_id,
        is_active=True,
    )
    db_session.add(target_group)
    await db_session.commit()
    await db_session.refresh(target_group)

    await clone_group_settings(sample_group.id, target_group.id, db_session)
    await db_session.commit()

    async with test_session_factory() as session:
        res = await session.execute(
            select(GroupConfig).where(
                GroupConfig.group_id == target_group.id,
                GroupConfig.key == "notify_admin_on_manual_review",
            )
        )
        cfg = res.scalar_one_or_none()
        assert cfg is not None
        assert cfg.value == "true"
        assert cfg.value_type == "bool"
