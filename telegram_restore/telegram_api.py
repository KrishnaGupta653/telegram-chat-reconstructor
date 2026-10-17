"""Telethon access: client setup, read-only import checks, and creating an empty import group."""
from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient, errors, types, utils
from telethon.tl import functions

from .config import Config

IMPORT_HEAD_LINES = 100
MAX_FLOOD_RETRIES = 3


class FloodStop(Exception):
    """Telegram asked for a longer wait than allowed (or kept asking); the caller should stop and save."""

    def __init__(self, seconds: int | None) -> None:
        super().__init__(seconds)
        self.seconds = seconds


async def call_with_flood(make_coro, max_wait: int, waits: list[int]):
    """Run one request; sit out FLOOD_WAITs up to `max_wait` seconds, at most MAX_FLOOD_RETRIES times."""
    for _ in range(MAX_FLOOD_RETRIES):
        try:
            return await make_coro()
        except errors.FloodWaitError as e:
            waits.append(e.seconds)
            if e.seconds > max_wait:
                raise FloodStop(e.seconds) from e
            print(f"FLOOD_WAIT {e.seconds}s: waiting as Telegram requested")
            await asyncio.sleep(e.seconds + 1)
    raise FloodStop(None)


def make_client(cfg: Config) -> TelegramClient:
    cfg.require_telegram()
    cfg.session.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(cfg.session), cfg.api_id, cfg.api_hash)


def import_head(chat_file: Path) -> str:
    with open(chat_file, encoding="utf-8") as f:
        return "".join(line for _, line in zip(range(IMPORT_HEAD_LINES), f))


async def probe(cfg: Config, chat_files: list[Path], peer: str | None) -> list[dict]:
    """messages.checkHistoryImport for each candidate file, optionally messages.checkHistoryImportPeer."""
    results = []
    client = make_client(cfg)
    await client.start()
    try:
        for path in chat_files:
            try:
                r = await client(functions.messages.CheckHistoryImportRequest(import_head=import_head(path)))
                results.append({"file": str(path), "pm": bool(r.pm), "group": bool(r.group), "title": r.title})
            except errors.FloodWaitError as e:
                results.append({"file": str(path), "error": f"FLOOD_WAIT {e.seconds}s; stopping probe"})
                break
            except errors.RPCError as e:
                results.append({"file": str(path), "error": f"{type(e).__name__}: {e}"})
        if peer:
            try:
                entity = await client.get_input_entity(int(peer) if peer.lstrip("-").isdigit() else peer)
                r = await client(functions.messages.CheckHistoryImportPeerRequest(peer=entity))
                results.append({"peer": peer, "confirm_text": r.confirm_text})
            except errors.RPCError as e:
                results.append({"peer": peer, "error": f"{type(e).__name__}: {e}"})
            except ValueError as e:
                results.append({"peer": peer, "error": f"cannot resolve peer ({e}); use @username or +phone"})
    finally:
        await client.disconnect()
    return results


async def upgrade_group(cfg: Config, chat_id: int) -> tuple[str, int]:
    """messages.migrateChat: basic group -> supergroup (irreversible); returns (title, new marked id)."""
    client = make_client(cfg)
    await client.start()
    try:
        entity = await client.get_entity(chat_id)
        if not isinstance(entity, types.Chat):
            raise SystemExit(f"{chat_id} is not a basic group.")
        r = await client(functions.messages.MigrateChatRequest(chat_id=entity.id))
        channel = next(c for c in r.chats if isinstance(c, types.Channel))
        return channel.title, utils.get_peer_id(channel)
    finally:
        await client.disconnect()


async def create_import_group(cfg: Config, title: str, about: str) -> int:
    """New empty supergroup flagged for history import; returns its marked id (-100...)."""
    client = make_client(cfg)
    await client.start()
    try:
        r = await client(functions.channels.CreateChannelRequest(title=title, about=about, megagroup=True,
                                                                 for_import=True))
        return utils.get_peer_id(r.chats[0])
    finally:
        await client.disconnect()
