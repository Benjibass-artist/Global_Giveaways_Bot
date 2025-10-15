#!/usr/bin/env python3
"""
Discord bot that periodically scans configured sources for giveaway links
and posts any new ones to a specific Discord channel.

Configuration via environment variables (set in .env or your environment):
  - DISCORD_TOKEN          (required) bot token
  - CHANNEL_ID             (required) channel id to post in (int)
  - SCAN_INTERVAL_MINUTES  (optional) default 12 minutes
    - SOURCES_FILE           (optional) default: sources.json
  - STATE_FILE             (optional) default: data/state.json

Commands:
  - none required; runs automatically. If you want a manual scan trigger, type
    "scan now" in the channel (requires MESSAGE CONTENT intent enabled) — optional.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Iterable, List, Dict, Set
import time

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# --- Minimal persistent state (tracks posted URLs) ---
import json as _json
from typing import Set as _Set


def _fmt_cooldown(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class State:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen = set()
        # usage rate-limit store: key -> list[timestamps]
        self._usage = {}
        # url -> list of {channel_id: int, message_id: int}
        self._posts = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = _json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "seen" in data and isinstance(data["seen"], list):
                self._seen = set(map(str, data["seen"]))
            elif isinstance(data, list):
                self._seen = set(map(str, data))
            # posts is optional for backward compatibility
            if isinstance(data, dict) and isinstance(data.get("posts"), dict):
                posts = {}
                for url, entries in data["posts"].items():
                    safe_entries = []
                    if isinstance(entries, list):
                        for rec in entries:
                            if isinstance(rec, dict) and str(rec.get("channel_id", "")).isdigit() and str(rec.get("message_id", "")).isdigit():
                                safe_entries.append({"channel_id": int(rec["channel_id"]), "message_id": int(rec["message_id"])})
                    posts[str(url)] = safe_entries
                self._posts = posts
            # usage is optional
            if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                usage: dict[str, list[float]] = {}
                for k, v in data["usage"].items():
                    if isinstance(v, list):
                        times: list[float] = []
                        for t in v:
                            try:
                                times.append(float(t))
                            except Exception:
                                continue
                        usage[str(k)] = times
                self._usage = usage
        except Exception:
            self._seen = set()
            self._posts = {}
            self._usage = {}

    def save(self):
        try:
            payload = {"seen": sorted(self._seen), "posts": self._posts, "usage": self._usage}
            self.path.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def remember(self, url: str):
        if url:
            self._seen.add(url)

    def seen(self, url: str) -> bool:
        return bool(url) and url in self._seen

    def record_post(self, url: str, channel_id: int, message_id: int):
        if not url:
            return
        lst = self._posts.setdefault(url, [])
        lst.append({"channel_id": int(channel_id), "message_id": int(message_id)})

    def posts_for(self, url: str) -> list[dict[str, int]]:
        return list(self._posts.get(url, []))

    def all_urls_with_posts(self) -> list[str]:
        return list(self._posts.keys())

    def remove_url(self, url: str):
        self._seen.discard(url)
        if url in self._posts:
            del self._posts[url]

    def remove_channel_posts(self, channel_id: int) -> bool:
        """Remove all recorded posts for a specific channel. Returns True if state changed."""
        changed = False
        for url in list(self._posts.keys()):
            entries = self._posts.get(url, [])
            new_entries = [e for e in entries if int(e.get("channel_id", 0)) != int(channel_id)]
            if len(new_entries) != len(entries):
                changed = True
                if new_entries:
                    self._posts[url] = new_entries
                else:
                    # remove the url entirely if no posts remain
                    del self._posts[url]
        return changed

    # --- Simple persisted rate limiter ---
    def _prune_usage(self, key: str, window_seconds: int, now: float) -> list[float]:
        entries = [t for t in self._usage.get(key, []) if now - t < window_seconds]
        self._usage[key] = entries
        return entries

    def allow(self, key: str, limit: int, window_seconds: int, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds). If allowed, usage is recorded immediately."""
        if now is None:
            now = time.time()
        entries = self._prune_usage(key, window_seconds, now)
        if len(entries) >= limit:
            oldest = min(entries) if entries else now
            retry_after = int(max(0, window_seconds - (now - oldest)))
            return False, retry_after
        entries.append(now)
        self._usage[key] = entries
        # Persist right away to make limits robust across restarts
        try:
            self.save()
        except Exception:
            pass
        return True, 0

    def has_post_in_channel(self, url: str, channel_id: int) -> bool:
        try:
            entries = self._posts.get(url, [])
            for rec in entries:
                if int(rec.get("channel_id", 0)) == int(channel_id):
                    return True
        except Exception:
            return False
        return False


# --- Scraper using Requests + BeautifulSoup ---
import re as _re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse as _urlparse, urlunparse as _urlunparse, urljoin as _urljoin

KEYWORDS = _re.compile(r"giveaway|contest|sweepstake|free\b|win\b|prize", _re.I)
HEADERS = {
    "User-Agent": "giveaway-bot/1.0 (+https://pypi.org/project/beautifulsoup4/) requests"
}


_GLEAM_HOST = _re.compile(r"^(www\.)?gleam\.io$", _re.I)
_GLEAM_SHORT_HOST = _re.compile(r"^(www\.)?wn\.nr$", _re.I)


def _normalize_url(u: str) -> str:
    try:
        p = _urlparse(u)
        # drop query and fragment for dedup/cleaner posting
        return _urlunparse((p.scheme or "https", p.netloc, p.path, "", "", ""))
    except Exception:
        return u


_GLEAM_BLOCKED_FIRST_SETS = {
    "blog", "features", "pricing", "help", "docs", "legal", "terms", "privacy",
    "jobs", "status", "login", "signup", "partners", "about", "contact", "press",
    "brand", "developer", "developers", "api", "company", "changelog", "site", "pages",
    "collections", "category", "categories", "tag", "tags", "gallery",
    "app", "tools", "guides", "customers", "success", "integrations", "faq", "", "templates"
}


def _is_likely_gleam_campaign(u: str) -> bool:
    try:
        p = _urlparse(u)
        host = (p.netloc or "").lower()
        # New simple rule: accept any gleam.io or wn.nr URL that is not a utility section
        if _GLEAM_HOST.match(host):
            parts = [seg for seg in (p.path or "").split("/") if seg]
            if parts and parts[0].lower() in _GLEAM_BLOCKED_FIRST_SETS:
                return False
            return True
        if _GLEAM_SHORT_HOST.match(host):
            return True
        return False
    except Exception:
        return False


def fetch_giveaway_links(source_url: str) -> list[dict]:
    log.debug("Fetching %s", source_url)
    resp = requests.get(source_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    if not resp.encoding:
        try:
            resp.encoding = resp.apparent_encoding  # type: ignore[attr-defined]
        except Exception:
            resp.encoding = "utf-8"
    html = resp.text

    soup = BeautifulSoup(html, features="html.parser")
    items: list[dict] = []
    total_anchors = 0
    if "gleam.io" in source_url:
        # Extract direct giveaway links from Gleam listings
        anchors = list(soup.find_all("a", href=True))
        total_anchors = len(anchors)
        for a in anchors:
            href = a["href"].strip()
            abs_url = _urljoin(source_url, href)
            norm = _normalize_url(abs_url)
            # Accept any non-utility gleam.io or any wn.nr link
            try:
                p = _urlparse(norm)
                host = (p.netloc or "").lower()
                parts = [seg for seg in (p.path or "").split("/") if seg]
            except Exception:
                host = ""
                parts = []
            if (_GLEAM_HOST.match(host) and (not parts or parts[0].lower() not in _GLEAM_BLOCKED_FIRST_SETS)) or _GLEAM_SHORT_HOST.match(host):
                text = (a.get_text(strip=True) or "").strip()
                title = text or a.get("title") or norm
                items.append({"title": title, "url": norm, "source": source_url})
    else:
        # Generic keyword-based extraction
        anchors = list(soup.find_all("a", href=True))
        total_anchors = len(anchors)
        for a in anchors:
            text = (a.get_text(strip=True) or "").strip()
            href = a["href"].strip()
            label = f"{text} {href}".strip()
            if KEYWORDS.search(label):
                abs_url = _urljoin(source_url, href)
                title = text or a.get("title") or abs_url
                items.append({"title": title, "url": abs_url, "source": source_url})

    # Deduplicate by URL
    seen = set()
    deduped = []
    for it in items:
        u = it.get("url")
        if u and u not in seen:
            seen.add(u)
            deduped.append(it)
    log.info("Parsed %s: anchors=%d, matches=%d", source_url, total_anchors, len(deduped))
    return deduped


def _is_gleam_expired(url: str) -> bool:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    except Exception:
        return False
    if resp.status_code >= 400:
        return True
    if not resp.encoding:
        try:
            resp.encoding = resp.apparent_encoding  # type: ignore[attr-defined]
        except Exception:
            resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, features="html.parser")
    # Heuristics for ended giveaways on Gleam
    text = soup.get_text(" ", strip=True)
    if _re.search(r"(ended|giveaway\s+has\s+ended|no\s+longer\s+active|expired)", text, _re.I):
        return True
    return False


def is_expired(url: str) -> bool:
    if "gleam.io" in url:
        return _is_gleam_expired(url)
    # Fallback heuristic for other sites
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if resp.status_code >= 400:
            return True
        if not resp.encoding:
            try:
                resp.encoding = resp.apparent_encoding  # type: ignore[attr-defined]
            except Exception:
                resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, features="html.parser")
        text = soup.get_text(" ", strip=True)
        if _re.search(r"(ended|expired|no\s+longer\s+active)", text, _re.I):
            return True
    except Exception:
        return False
    return False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("giveaway-bot")


def load_config() -> dict:
    load_dotenv()
    cfg = {
        "token": os.getenv("DISCORD_TOKEN", ""),
        "interval_minutes": float(os.getenv("SCAN_INTERVAL_MINUTES", "12")),
        "sources_file": os.getenv("SOURCES_FILE", "sources.json"),
        "state_file": os.getenv("STATE_FILE", "data/state.json"),
        "channels_file": os.getenv("CHANNELS_FILE", "data/channels.json"),
    }
    return cfg


def load_sources(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # support either {"sources": [..]} or [..]
        if isinstance(data, dict) and "sources" in data:
            return [str(u) for u in data["sources"]]
        elif isinstance(data, list):
            return [str(u) for u in data]
    except Exception as e:
        log.error("Failed to load sources from %s: %s", p, e)
    return []


class ChannelsConfig:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._channels: Dict[str, int] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "channels" in data and isinstance(data["channels"], dict):
                self._channels = {str(k): int(v) for k, v in data["channels"].items() if str(v).isdigit()}
        except Exception:
            self._channels = {}

    def save(self):
        try:
            payload = {"channels": {k: v for k, v in self._channels.items()}}
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def set_channel(self, guild_id: int, channel_id: int | None):
        if channel_id is None:
            self._channels.pop(str(guild_id), None)
        else:
            self._channels[str(guild_id)] = int(channel_id)

    def get_channel(self, guild_id: int) -> int | None:
        v = self._channels.get(str(guild_id))
        return int(v) if v is not None else None

    def all_channel_ids(self) -> List[int]:
        return [int(v) for v in self._channels.values()]


class GiveawayBot(discord.Client):
    def __init__(self, *, intents: discord.Intents, cfg: dict):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.cfg = cfg
        self.state = State(Path(cfg["state_file"]))
        self.channels = ChannelsConfig(Path(cfg["channels_file"]))
        self.sources = []
        self.scan_task = self._scan_loop
        self._synced_on_ready = False

    async def setup_hook(self):
        # load sources on startup
        self.sources = load_sources(self.cfg["sources_file"]) or []
        if not self.sources:
            log.warning("No sources configured. Create sources.json with a list of URLs to scan.")
        else:
            log.info("Loaded %d source(s).", len(self.sources))

        # start background loops (24h cadence configured on decorators)
        self._scan_loop.start()
        self._cleanup_loop.start()
        log.info("Started background loops: scan(24h), cleanup(24h)")

        # Slash command to set channel per guild (requires Manage Server permission)
        @self.tree.command(name="setchannel", description="Set this channel for giveaway posts")
        @app_commands.checks.has_permissions(manage_guild=True)
        async def setchannel(interaction: discord.Interaction):
            # rate limit: per-guild, 2 times per 24h
            if interaction.guild:
                key = f"setchannel:guild:{interaction.guild.id}"
                allowed, retry = self.state.allow(key, limit=2, window_seconds=24*3600)
                if not allowed:
                    await interaction.response.send_message(
                        f"Cooldown active. Try again in {_fmt_cooldown(retry)}.", ephemeral=True
                    )
                    return
            if not interaction.guild or not interaction.channel:
                await interaction.response.send_message("Use this command in a server text channel.", ephemeral=True)
                return
            ch = interaction.channel
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message("Please use this in a text channel.", ephemeral=True)
                return
            channel_id = ch.parent_id if isinstance(ch, discord.Thread) else ch.id
            self.channels.set_channel(interaction.guild.id, channel_id)
            self.channels.save()
            await interaction.response.send_message(f"Giveaway posts will be sent to <#{channel_id}>.", ephemeral=True)

        # Ensure commands are registered only once
        if not hasattr(self, '_commands_registered'):
            self._commands_registered = True

            # Slash command to trigger an immediate scan in the current channel
            @self.tree.command(name="scan", description="Trigger an immediate giveaway scan in this channel")
            @app_commands.checks.has_permissions(manage_guild=True)
            async def scan_now(interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                if not interaction.channel:
                    await interaction.followup.send("Use this command in a server text channel.", ephemeral=True)
                    return
                ch = interaction.channel
                if not isinstance(ch, discord.TextChannel):
                    await interaction.followup.send("Please use this in a text channel.", ephemeral=True)
                    return
                # rate limit: per-channel, once per 24h
                key = f"scan:channel:{ch.id}"
                allowed, retry = self.state.allow(key, limit=1, window_seconds=24*3600)
                if not allowed:
                    await interaction.followup.send(
                        f"This channel hit its daily scan limit. Try again in {_fmt_cooldown(retry)}.", ephemeral=True
                    )
                    return
                channel_id = ch.id
                try:
                    batch: list[dict] = []
                    for src in self.sources:
                        items = await asyncio.to_thread(fetch_giveaway_links, src)
                        if items:
                            batch.extend(items)
                    if batch:
                        await self._post_items_to_channel(channel_id, batch)
                        self.state.save()
                        await interaction.followup.send(f"Scan complete. Posted items to <#{channel_id}>.", ephemeral=True)
                    else:
                        await interaction.followup.send("No new giveaways found.", ephemeral=True)
                except Exception as e:
                    await interaction.followup.send(f"Scan failed: {e}", ephemeral=True)

        # Slash command to preview extraction results (rate limited: per-channel once per 24h)
        @self.tree.command(name="preview", description="Show what the scraper finds for a source URL")
        @app_commands.describe(url="Optional URL; defaults to the configured sources")
        async def preview(interaction: discord.Interaction, url: str | None = None):
            await interaction.response.defer(ephemeral=True)
            if interaction.channel:
                key = f"preview:channel:{interaction.channel.id}"
                allowed, retry = self.state.allow(key, limit=1, window_seconds=24*3600)
                if not allowed:
                    await interaction.followup.send(
                        f"This channel hit its daily preview limit. Try again in {_fmt_cooldown(retry)}.", ephemeral=True
                    )
                    return
            targets = [url] if url else list(self.sources)
            if not targets:
                await interaction.followup.send("No sources configured.", ephemeral=True)
                return
            lines: list[str] = []
            for u in targets[:3]:
                try:
                    items = await asyncio.to_thread(fetch_giveaway_links, u)
                except Exception as e:
                    lines.append(f"- {u}: error {e}")
                    continue
                hint = ""
                if "gleam.io" in (u or "") and not items:
                    hint = " (gleam filtered: 0 kept)"
                lines.append(f"- {u}: {len(items)} found{hint}")
                for it in items[:5]:
                    lines.append(f"  • {it.get('title') or it.get('url')} -> {it.get('url')}")
            text = "\n".join(lines) or "No results."
            await interaction.followup.send(text, ephemeral=True)

        # Slash command to start bot activity in the current channel
        @self.tree.command(name="start", description="Start the bot's activity in this channel")
        @app_commands.checks.has_permissions(manage_guild=True)
        async def start(interaction: discord.Interaction):
            if not interaction.guild or not interaction.channel:
                await interaction.response.send_message("Use this command in a server text channel.", ephemeral=True)
                return
            ch = interaction.channel
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message("Please use this in a text channel.", ephemeral=True)
                return
            channel_id = ch.parent_id if isinstance(ch, discord.Thread) else ch.id
            self.channels.set_channel(interaction.guild.id, channel_id)
            self.channels.save()
            await interaction.response.send_message(f"Bot activity started in <#{channel_id}>.", ephemeral=True)

        # Slash command to stop bot activity in the current channel
        @self.tree.command(name="stop", description="Stop the bot's activity in this channel")
        @app_commands.checks.has_permissions(manage_guild=True)
        async def stop(interaction: discord.Interaction):
            if not interaction.guild or not interaction.channel:
                await interaction.response.send_message("Use this command in a server text channel.", ephemeral=True)
                return
            ch = interaction.channel
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message("Please use this in a text channel.", ephemeral=True)
                return
            channel_id = ch.parent_id if isinstance(ch, discord.Thread) else ch.id
            if self.channels.get_channel(interaction.guild.id) == channel_id:
                self.channels.set_channel(interaction.guild.id, None)
                self.channels.save()
                await interaction.response.send_message(f"Bot activity stopped in <#{channel_id}>.", ephemeral=True)
            else:
                await interaction.response.send_message("This channel is not currently active.", ephemeral=True)

        # Slash command to clear all bot messages in the configured channel
        @self.tree.command(name="clear", description="Clear all giveaway posts sent by this bot in the configured channel")
        @app_commands.checks.has_permissions(manage_guild=True)
        async def clear_cmd(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            if not interaction.guild:
                await interaction.followup.send("Use this command in a server.", ephemeral=True)
                return
            channel_id = self.channels.get_channel(interaction.guild.id)
            if not channel_id:
                await interaction.followup.send("No channel configured. Use /setchannel in the target channel.", ephemeral=True)
                return
            # Resolve channel
            ch = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not isinstance(ch, discord.TextChannel):
                await interaction.followup.send("Configured channel is not a text channel.", ephemeral=True)
                return

            # Delete recorded posts first
            to_delete: list[tuple[str, int]] = []
            for url, entries in list(self.state._posts.items()):
                for rec in entries:
                    if int(rec.get("channel_id", 0)) == int(channel_id):
                        mid = rec.get("message_id")
                        if mid:
                            to_delete.append((url, int(mid)))

            deleted = 0
            for _url, mid in to_delete:
                try:
                    msg = await ch.fetch_message(mid)
                    if self.user and msg.author.id == self.user.id:
                        await msg.delete()
                        deleted += 1
                        await asyncio.sleep(0.3)
                except Exception:
                    # ignore messages that no longer exist or can't be fetched
                    continue

            # Best-effort sweep to catch any untracked bot messages
            extra_deleted = 0
            try:
                async for msg in ch.history(limit=500):
                    try:
                        if self.user and msg.author.id == self.user.id:
                            await msg.delete()
                            extra_deleted += 1
                            await asyncio.sleep(0.25)
                    except Exception:
                        continue
            except Exception:
                pass

            # Prune state mapping for this channel
            if self.state.remove_channel_posts(channel_id):
                self.state.save()

            await interaction.followup.send(
                f"Cleared {deleted + extra_deleted} message(s) from <#{channel_id}>.", ephemeral=True
            )

        # Help: visible to everyone; shows which commands are admin-only
        @self.tree.command(name="help", description="Show available commands for this bot")
        async def help_cmd(interaction: discord.Interaction):
            lines = [
                "Commands:",
                "• /preview — Show what the scraper finds (per-channel 1/day).",
                "",
                "Admin-only (Manage Server):",
                "• /setchannel — Set the channel to receive giveaway posts.",
                "• /start — Start bot activity in this channel.",
                "• /stop — Stop bot activity in this channel.",
                "• /scan — Manually scan now (per-channel 1/day).",
                "• /clear — Delete the bot's giveaway posts in the configured channel.",
                "",
                "Background jobs: scan and cleanup run every 24 hours.",
            ]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        # Sync application commands after registering all of them
        try:
            if self.guilds:
                for guild in self.guilds:
                    try:
                        self.tree.copy_global_to(guild=guild)
                        await self.tree.sync(guild=guild)
                        log.info("Synced application commands to guild %s", guild.id)
                    except Exception as e:
                        log.warning("Guild sync failed for %s: %s", getattr(guild, 'id', '?'), e)
            else:
                await self.tree.sync()
                log.info("Synced global application commands")
        except Exception as e:
            log.warning("Failed to sync commands: %s", e)

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user and self.user.id)
        guild_ids = [g.id for g in self.guilds]
        log.info("Connected guilds (%d): %s", len(guild_ids), guild_ids)
        chan_ids = self.channels.all_channel_ids()
        if chan_ids:
            log.info("Configured post channels (%d): %s", len(chan_ids), chan_ids)
        else:
            log.warning("No channels configured. Use /setchannel in the desired channel.")
        # One-time per-guild sync so new commands (like /help) appear immediately
        if not self._synced_on_ready and self.guilds:
            try:
                for guild in self.guilds:
                    try:
                        self.tree.copy_global_to(guild=guild)
                        await self.tree.sync(guild=guild)
                        log.info("Synced commands to guild %s on ready", guild.id)
                    except Exception as e:
                        log.warning("Guild sync on ready failed for %s: %s", getattr(guild, 'id', '?'), e)
                self._synced_on_ready = True
            except Exception as e:
                log.warning("Bulk on_ready sync failed: %s", e)

    async def on_guild_join(self, guild: discord.Guild):
        # Sync commands for newly joined guilds
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to new guild %s", guild.id)
        except Exception as e:
            log.warning("Failed to sync commands on guild join for %s: %s", getattr(guild, 'id', '?'), e)

    @tasks.loop(hours=24)
    async def _scan_loop(self):
        # wait until bot is ready and channel resolved
        if not self.is_ready():
            return
        active_channels = [ch for ch in self.channels.all_channel_ids() if ch]
        if not active_channels:
            return
        if not self.sources:
            return
        await self._scan_once()

    async def _scan_once(self) -> int:
        log.info("Scanning %d source(s) for new giveaways…", len(self.sources))
        new_items: list[dict] = []
        for url in self.sources:
            try:
                items = await asyncio.to_thread(fetch_giveaway_links, url)
            except Exception as e:
                log.warning("Error scraping %s: %s", url, e)
                continue
            if not items:
                continue
            # Do not use global seen; per-channel filtering happens at post time
            new_items.extend(items)
        if new_items:
            await self._post_items_all_channels(new_items)
            self.state.save()
            return len(new_items)
        else:
            log.info("No new giveaways found.")
            return 0

    async def _post_items_all_channels(self, items: list[dict]):
        # Post each item to all configured channels, avoiding duplicates within each channel
        for channel_id in self.channels.all_channel_ids():
            ch = self.get_channel(channel_id)
            if not isinstance(ch, discord.TextChannel):
                try:
                    ch = await self.fetch_channel(channel_id)
                except Exception as e:
                    log.warning("Unable to fetch channel %s: %s", channel_id, e)
                    continue
            if not isinstance(ch, discord.TextChannel):
                continue
            existing_messages = [msg async for msg in ch.history(limit=200)]
            existing_links = {msg.content.splitlines()[1] for msg in existing_messages if len(msg.content.splitlines()) > 1}
            for it in items:
                title = it.get("title") or it.get("url") or "Giveaway"
                link = it.get("url")
                if not link:
                    continue
                if link in existing_links or self.state.has_post_in_channel(link, ch.id):
                    log.debug("Duplicate in channel %s: %s", channel_id, link)
                    continue
                content = f"🎁 {title}\n{link}"
                try:
                    msg = await ch.send(content)
                    self.state.record_post(link, ch.id, msg.id)
                    # Do not mark globally seen; keep per-channel behavior
                    await asyncio.sleep(1.2)
                except Exception as e:
                    log.warning("Failed to post item %s to channel %s: %s", link, channel_id, e)

    async def _post_items_to_channel(self, channel_id: int, items: list[dict]):
        # Post items to a specific channel, avoiding duplicates
        ch = self.get_channel(channel_id)
        if not isinstance(ch, discord.TextChannel):
            try:
                ch = await self.fetch_channel(channel_id)
            except Exception as e:
                log.warning("Unable to fetch channel %s: %s", channel_id, e)
                return
        if not isinstance(ch, discord.TextChannel):
            return
        existing_messages = [msg async for msg in ch.history(limit=200)]
        existing_links = {msg.content.splitlines()[1] for msg in existing_messages if len(msg.content.splitlines()) > 1}
        posted = 0
        for it in items:
            title = it.get("title") or it.get("url") or "Giveaway"
            link = it.get("url")
            if not link:
                continue
            if link in existing_links or self.state.has_post_in_channel(link, ch.id):
                continue
            content = f"🎁 {title}\n{link}"
            try:
                msg = await ch.send(content)
                self.state.record_post(link, ch.id, msg.id)
                posted += 1
                await asyncio.sleep(1.2)
            except Exception as e:
                log.warning("Failed to post item %s to channel %s: %s", link, channel_id, e)
        log.info("Posted %d item(s) to channel %s", posted, channel_id)

    async def on_message(self, message: discord.Message):
        # Optional manual trigger via "scan now" in the target channel; requires message content intent
        if message.author.bot:
            return
        if message.channel.id not in set(self.channels.all_channel_ids()):
            return
        if "scan now" in message.content.lower():
            await message.add_reaction("🔎")
            await self._scan_once()

    @tasks.loop(hours=24)
    async def _cleanup_loop(self):
        if not self.is_ready():
            return
        active_channels = [ch for ch in self.channels.all_channel_ids() if ch]
        if not active_channels:
            return
        urls = list(self.state.all_urls_with_posts())
        if not urls:
            return
        log.info("Cleanup: checking %d posted URL(s) for expiry…", len(urls))
        removed = 0
        for url in urls:
            try:
                expired = await asyncio.to_thread(is_expired, url)
            except Exception:
                expired = False
            if not expired:
                continue
            # delete messages for this url
            posts = self.state.posts_for(url)
            for rec in posts:
                channel_id = rec.get("channel_id")
                message_id = rec.get("message_id")
                if not channel_id or not message_id or channel_id not in active_channels:
                    continue
                ch = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                if isinstance(ch, discord.TextChannel):
                    try:
                        msg = await ch.fetch_message(message_id)
                        await msg.delete()
                        removed += 1
                        await asyncio.sleep(0.6)
                    except Exception as e:
                        log.debug("Cleanup: could not delete msg %s in %s: %s", message_id, channel_id, e)
            self.state.remove_url(url)
            self.state.save()
        if removed:
            log.info("Cleanup: removed %d expired post(s).", removed)



def main() -> int:
    cfg = load_config()
    token = cfg.get("token")
    if not token:
        log.error("DISCORD_TOKEN not set. Create a .env file and set DISCORD_TOKEN.")
        return 1

    intents = discord.Intents.default()
    # No privileged intents required. Use /scan and /setchannel slash commands.

    bot = GiveawayBot(intents=intents, cfg=cfg)
    try:
        bot.run(token)
    except KeyboardInterrupt:
        log.info("Shutting down…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
