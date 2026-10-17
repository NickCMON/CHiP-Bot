"""Owner-only, reversible Discord channel lockdown for CHiP.

Does not alter the existing /maintenance bot-service commands.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

log = logging.getLogger("chip.lockdown")
lockdown_group = app_commands.Group(name="lockdown", description="Owner-only server redesign lockdown")
_operation_lock = asyncio.Lock()


def _backup_path(data_dir: Path, guild_id: int) -> Path:
    return data_dir / f"chip_lockdown_{guild_id}.json"


def _snapshot_channel(channel):
    overwrites = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites.append({"id": target.id, "kind": "role" if isinstance(target, discord.Role) else "member", "allow": allow.value, "deny": deny.value})
    return {"id": channel.id, "name": channel.name, "synced": bool(getattr(channel, "permissions_synced", False)), "overwrites": overwrites}


def _restore_overwrites(guild, entry):
    result = {}
    for item in entry["overwrites"]:
        target = guild.get_role(item["id"]) if item["kind"] == "role" else guild.get_member(item["id"])
        if target is None and item["kind"] == "member":
            target = discord.Object(id=item["id"], type=discord.Member)
        if target is None:
            raise RuntimeError(f"Role {item['id']} was deleted; restore it or ask for help before retrying.")
        allow = discord.Permissions(item["allow"])
        deny = discord.Permissions(item["deny"])
        result[target] = discord.PermissionOverwrite.from_pair(allow, deny)
    return result


def _save_backup(path, snapshot):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(snapshot, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def _load_backup(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _channel_lock_overwrites(guild, channel, announcement):
    ow = dict(channel.overwrites)
    everyone = guild.default_role
    base = ow.get(everyone, discord.PermissionOverwrite())
    base.view_channel = True if announcement else False
    if announcement:
        base.send_messages = False
        base.send_messages_in_threads = False
        base.create_public_threads = False
        base.create_private_threads = False
        base.add_reactions = False
        base.connect = False
        base.speak = False
    ow[everyone] = base
    for target, previous in list(ow.items()):
        if target.id == everyone.id:
            continue
        updated = discord.PermissionOverwrite.from_pair(*previous.pair())
        if announcement:
            if updated.view_channel is False:
                updated.view_channel = True
            if updated.send_messages is True:
                updated.send_messages = False
            if updated.send_messages_in_threads is True:
                updated.send_messages_in_threads = False
            if updated.add_reactions is True:
                updated.add_reactions = False
            if updated.connect is True:
                updated.connect = False
        else:
            if updated.view_channel is True:
                updated.view_channel = False
        ow[target] = updated
    return ow


async def _owner(interaction):
    return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id


async def _reply(interaction, message):
    if interaction.response.is_done():
        await interaction.followup.send(message[:1900], ephemeral=True)
    else:
        await interaction.response.send_message(message[:1900], ephemeral=True)


async def _preflight(interaction, announcement_id, data_dir):
    guild = interaction.guild
    channel = guild.get_channel(announcement_id)
    if not isinstance(channel, discord.TextChannel):
        return "The configured CHiP announcement channel is missing or is not a text channel."
    if not guild.me or not guild.me.guild_permissions.administrator:
        return "CHiP needs Administrator permission to safely manage and recover all channel overrides."
    admin_roles = [role.name for role in guild.roles if role.permissions.administrator and role not in guild.me.roles]
    if admin_roles:
        return "Other Administrator roles bypass channel lockdown: " + ', '.join(admin_roles[:12]) + ". Disable their Administrator permission first."
    # A role with Administrator shared by another member is also unsafe.
    for role in guild.me.roles:
        if role.permissions.administrator and any(member.id != guild.me.id for member in role.members):
            return f"CHiP's Administrator role ({role.name}) is shared with other members. Give CHiP a private admin role first."
    if not data_dir.is_dir() or not os.access(data_dir, os.W_OK):
        return "CHiP's persistent data directory is not writable. Check Railway's /data volume."
    if str(data_dir.resolve()) == str(Path(__file__).resolve().parent):
        return "CHiP's data directory is inside the app deployment, not persistent. Mount Railway volume at /data and set CHIP_DATA_DIR=/data first."
    if len(guild.channels) == 0:
        return "Discord returned no channels; lockdown cancelled."
    return None


def register(bot, announcement_id: int, data_dir: Path):
    @lockdown_group.command(name="status", description="Check whether server channel lockdown is active")
    async def status(interaction: discord.Interaction):
        if not await _owner(interaction):
            return await _reply(interaction, "Only the server owner can use server lockdown.")
        path = _backup_path(data_dir, interaction.guild.id)
        if not path.exists():
            return await _reply(interaction, "🔓 No saved lockdown is active. Server permissions have not been changed by /lockdown.")
        snap = _load_backup(path)
        await _reply(interaction, f"🔒 Lockdown backup exists ({snap['started_at']}). {len(snap['channels'])} channels saved. Use /lockdown end confirm:true to restore.")

    @lockdown_group.command(name="start", description="Hide all channels except the read-only maintenance channel")
    @app_commands.describe(confirm="Set true to apply the lockdown after reading the preview")
    async def start(interaction: discord.Interaction, confirm: bool = False):
        if not await _owner(interaction):
            return await _reply(interaction, "Only the server owner can start lockdown.")
        async with _operation_lock:
            guild = interaction.guild
            path = _backup_path(data_dir, guild.id)
            if path.exists():
                return await _reply(interaction, "A lockdown backup already exists. Use /lockdown status or /lockdown end; refusing to overwrite the backup.")
            error = await _preflight(interaction, announcement_id, data_dir)
            if error:
                return await _reply(interaction, "⚠️ Lockdown NOT started. " + error)
            if not confirm:
                return await _reply(interaction, f"PREVIEW ONLY — no changes made. Lockdown will hide {len(guild.channels)-1} channels from members, keep CHiP Announcements visible and read-only as 🔧server-maintenance, and save original permissions for restoration. Existing /maintenance commands are separate. Run /lockdown start confirm:true to proceed. IMPORTANT: Keep a separate backup of your server permissions and verify CHiP's Railway /data volume first.")
            await interaction.response.defer(ephemeral=True, thinking=True)
            channels = list(guild.channels)
            snapshot = {"version": 1, "guild_id": guild.id, "started_at": datetime.now(timezone.utc).isoformat(), "announcement_id": announcement_id, "channels": [_snapshot_channel(c) for c in channels]}
            try:
                _save_backup(path, snapshot)
            except OSError as exc:
                return await _reply(interaction, f"Could not save backup; no changes made: {exc}")
            changed = []
            try:
                # Categories first, then children; originals are saved for both.
                for channel in sorted(channels, key=lambda c: 0 if isinstance(c, discord.CategoryChannel) else 1):
                    await channel.edit(overwrites=_channel_lock_overwrites(guild, channel, channel.id == announcement_id), reason="CMON owner-authorized temporary server lockdown")
                    changed.append(channel.id)
                announcement = guild.get_channel(announcement_id)
                await announcement.edit(name="🔧server-maintenance", reason="CMON temporary server lockdown")
            except Exception as exc:
                log.exception("Lockdown partially applied; backup retained for /lockdown end")
                return await _reply(interaction, f"⚠️ Lockdown partially applied ({len(changed)}/{len(channels)} channels): {type(exc).__name__}. Backup is saved. Run /lockdown end confirm:true to restore immediately. Do not delete the /data volume.")
            await _reply(interaction, f"🔒 Lockdown applied to {len(channels)} channels. Only the server owner and Discord Administrators can bypass the hidden-channel restrictions. Announcement channel is read-only for members. Use /lockdown end confirm:true to restore.")

    @lockdown_group.command(name="end", description="Restore saved channel permissions and announcement name")
    @app_commands.describe(confirm="Set true to restore all original channel permissions")
    async def end(interaction: discord.Interaction, confirm: bool = False):
        if not await _owner(interaction):
            return await _reply(interaction, "Only the server owner can end lockdown.")
        async with _operation_lock:
            guild = interaction.guild
            path = _backup_path(data_dir, guild.id)
            if not path.exists():
                return await _reply(interaction, "No lockdown backup found. No permissions were changed.")
            snap = _load_backup(path)
            if snap.get('guild_id') != guild.id:
                return await _reply(interaction, "Backup guild ID does not match. No changes made.")
            if not confirm:
                return await _reply(interaction, f"PREVIEW ONLY — restore permissions and original names for {len(snap['channels'])} channels. Run /lockdown end confirm:true to restore.")
            await interaction.response.defer(ephemeral=True, thinking=True)
            failures = []
            # Restore categories first, then children. Preserve sync state.
            ordered = sorted(snap['channels'], key=lambda e: 0 if isinstance(guild.get_channel(e['id']), discord.CategoryChannel) else 1)
            for entry in ordered:
                channel = guild.get_channel(entry['id'])
                if channel is None:
                    failures.append(f"missing channel {entry['id']}")
                    continue
                try:
                    original = _restore_overwrites(guild, entry)
                    await channel.edit(overwrites=original, reason="CMON restore pre-lockdown channel permissions")
                    if channel.name != entry['name']:
                        await channel.edit(name=entry['name'], reason="CMON restore original channel name")
                    if entry['synced'] and getattr(channel, 'category', None) is not None:
                        await channel.edit(sync_permissions=True, reason="CMON restore category permission sync")
                except Exception as exc:
                    failures.append(f"{entry['id']}: {type(exc).__name__}")
                    log.exception("Failed restoring channel %s", entry['id'])
            if failures:
                return await _reply(interaction, f"⚠️ Restore incomplete. Backup kept so you can retry. {len(failures)} errors: " + ', '.join(failures[:8]))
            path.unlink()
            await _reply(interaction, f"🔓 Restored {len(ordered)} channels, their permissions, original names, and category sync. Lockdown backup cleared.")

    bot.tree.add_command(lockdown_group)
