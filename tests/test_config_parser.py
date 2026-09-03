import pytest
from database.models import GroupConfig
from bot.handlers.join_requests import _get_config_value


@pytest.mark.asyncio
async def test_get_config_value_missing_returns_default(db_session, sample_group):
    val = await _get_config_value(sample_group.id, "non_existent_key", "default_val", db_session)
    assert val == "default_val"


@pytest.mark.asyncio
async def test_get_config_value_none_value_returns_default(db_session, sample_group):
    db_session.add(GroupConfig(group_id=sample_group.id, key="null_key", value=None, value_type="string"))
    await db_session.commit()

    val = await _get_config_value(sample_group.id, "null_key", "fallback", db_session)
    assert val == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_val,expected", [
    ("false", False),
    ("False", False),
    ("FALSE", False),
    ("0", False),
    ("no", False),
    ("off", False),
    ("unexpected_string", False),
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("YES", True),
])
async def test_get_config_value_bool_parsing(db_session, sample_group, stored_val, expected):
    cfg = GroupConfig(group_id=sample_group.id, key="test_bool", value=stored_val, value_type="bool")
    db_session.add(cfg)
    await db_session.commit()

    val = await _get_config_value(sample_group.id, "test_bool", None, db_session)
    assert val is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_val,expected", [
    ("60.0", 60.0),
    ("30", 30.0),
    ("-100.5", -100.5),
    ("0.0", 0.0),
])
async def test_get_config_value_float_parsing(db_session, sample_group, stored_val, expected):
    cfg = GroupConfig(group_id=sample_group.id, key="test_float", value=stored_val, value_type="float")
    db_session.add(cfg)
    await db_session.commit()

    val = await _get_config_value(sample_group.id, "test_float", 0.0, db_session)
    assert val == expected
    assert isinstance(val, float)


@pytest.mark.asyncio
async def test_get_config_value_invalid_float_returns_default(db_session, sample_group):
    cfg = GroupConfig(group_id=sample_group.id, key="bad_float", value="not_a_number", value_type="float")
    db_session.add(cfg)
    await db_session.commit()

    val = await _get_config_value(sample_group.id, "bad_float", 99.9, db_session)
    assert val == 99.9


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_val,expected", [
    ("10", 10),
    ("0", 0),
    ("-5", -5),
])
async def test_get_config_value_int_parsing(db_session, sample_group, stored_val, expected):
    cfg = GroupConfig(group_id=sample_group.id, key="test_int", value=stored_val, value_type="int")
    db_session.add(cfg)
    await db_session.commit()

    val = await _get_config_value(sample_group.id, "test_int", 0, db_session)
    assert val == expected
    assert isinstance(val, int)


@pytest.mark.asyncio
async def test_get_config_value_invalid_int_returns_default(db_session, sample_group):
    cfg = GroupConfig(group_id=sample_group.id, key="bad_int", value="abc", value_type="int")
    db_session.add(cfg)
    await db_session.commit()

    val = await _get_config_value(sample_group.id, "bad_int", 42, db_session)
    assert val == 42


@pytest.mark.asyncio
async def test_get_config_value_string(db_session, sample_group):
    cfg = GroupConfig(group_id=sample_group.id, key="msg", value="Welcome to the group!", value_type="string")
    db_session.add(cfg)
    await db_session.commit()

    val = await _get_config_value(sample_group.id, "msg", "", db_session)
    assert val == "Welcome to the group!"
