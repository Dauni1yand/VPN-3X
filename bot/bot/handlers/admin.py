"""Admin-only commands: node management. Gated by `settings.admin_ids`
(README, Работа тг-бота -> Вариант админа)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings
from bot.services.api_client import server_api

router = Router(name="admin")
router.message.filter(lambda message: message.from_user.id in settings.admin_ids)


@router.message(Command("nodes"))
async def cmd_list_nodes(message: Message) -> None:
    nodes = await server_api.list_nodes()
    if not nodes:
        await message.answer("Нод пока нет.")
        return
    lines = [f"{n['name']} ({n['ip']}) -- {n['status']}" for n in nodes]
    await message.answer("\n".join(lines))


@router.message(Command("addnode"))
async def cmd_add_node(message: Message) -> None:
    # Usage: /addnode <name> <ip> <panel_base_url> <login> <password>
    parts = (message.text or "").split(maxsplit=5)[1:]
    if len(parts) != 5:
        await message.answer(
            "Использование: /addnode <name> <ip> <panel_base_url> <login> <password>"
        )
        return

    name, ip, panel_base_url, login, password = parts
    node = await server_api.add_node(name, ip, panel_base_url, login, password)
    await message.answer(f"Нода добавлена: {node['id']}")


@router.message(Command("delnode"))
async def cmd_delete_node(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /delnode <node_id>")
        return
    await server_api.delete_node(parts[1])
    await message.answer("Нода удалена.")
