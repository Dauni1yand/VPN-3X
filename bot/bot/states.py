"""FSM state groups for the bot's button-driven flows.

Every multi-value action (adding a node, changing the price, ...) is a
short wizard: the bot asks for one value at a time with an explicit prompt
and an example, instead of expecting the admin to remember a command's
argument order."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddNode(StatesGroup):
    """Bare VPS -> serving node, over SSH."""

    name = State()
    ip = State()
    ssh_password = State()
    country = State()


class ConnectNode(StatesGroup):
    """A server that already runs 3x-ui -- we just point at its panel."""

    name = State()
    ip = State()
    panel_url = State()
    login = State()
    password = State()
    country = State()


class IssueConfig(StatesGroup):
    telegram_id = State()
    hours = State()
    node = State()


class MigrateClient(StatesGroup):
    client_id = State()
    node = State()


class SetPrice(StatesGroup):
    amount = State()
    asset = State()


class SetAdDurations(StatesGroup):
    short = State()
    long = State()


class SingleValue(StatesGroup):
    """Shared by every one-field setting (subscription length, alert
    threshold, API tokens...). Which setting is being edited lives in the
    FSM data under "key", so these don't each need their own group."""

    value = State()


class ConnectCloudflare(StatesGroup):
    record_name = State()
    server_ip = State()


class Support(StatesGroup):
    message = State()
