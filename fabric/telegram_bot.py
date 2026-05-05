"""Telegram bot — the only human control surface in Phase 1.

Long-poll based (no inbound HTTP needed). Single hardcoded `chat_id`
auth: any update from a different chat is dropped silently. Slash
commands and inline-button callbacks both go through the same REST
surface that 1E exposed, via an in-process `httpx.AsyncClient` pointed
at our own server. That keeps the TG bot and the future web dashboard
on identical contracts.

Notifications are DB-driven, not pubsub: a 5s polling loop scans
`state.list_unacked_notifications()`, sends each as a Telegram message
with action buttons, records the resulting (chat_id, message_id) so a
reply-to-message can be reverse-mapped back to (project, issue), then
acknowledges the row. Polling is deliberate — at our human-scale
throughput a 5s lag is invisible and the contract stays "the DB is
source-of-truth, the bot is one of N possible delivery surfaces."

Tests exercise the pure formatters and the handler methods directly
with a mocked `context.bot`; the python-telegram-bot Application
itself is constructed only when `start()` is called in production.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from fabric import state


log = logging.getLogger("fabric.telegram_bot")


# ---- callback_data ----
#
# Telegram caps callback_data at 64 bytes. We use ":"-joined fields:
#   <action>:<project>:<number>
# Project names are validated by `fabric/config.py::Project.name` (no `:`).
# Issue numbers are ints. Total well under 64 bytes for realistic names.

_CALLBACK_PREFIX = "af"  # agent-fabric namespace


def encode_callback(action: str, project: str, number: int) -> str:
    if ":" in action or ":" in project:
        raise ValueError(f"action/project must not contain ':': {action!r}, {project!r}")
    return f"{_CALLBACK_PREFIX}:{action}:{project}:{number}"


@dataclass(frozen=True)
class CallbackPayload:
    action: str
    project: str
    number: int


def decode_callback(data: str) -> CallbackPayload | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != _CALLBACK_PREFIX:
        return None
    try:
        return CallbackPayload(action=parts[1], project=parts[2], number=int(parts[3]))
    except ValueError:
        return None


# ---- pure formatters ----


async def render_queue(rest: httpx.AsyncClient) -> str:
    r = await rest.get("/api/issues")
    r.raise_for_status()
    issues = r.json()
    if not issues:
        return "Queue: empty."
    lines = ["📋 Queue:"]
    for i in issues[:30]:
        priority = i.get("priority_label") or ""
        state_label = i.get("state_label") or "?"
        lines.append(
            f"  {i['project']}#{i['number']} [{state_label}] "
            f"{priority} — {i.get('title') or '?'}"
        )
    return "\n".join(lines)


async def render_status(rest: httpx.AsyncClient) -> str:
    s = (await rest.get("/api/status")).raise_for_status().json()
    paused = "⏸ paused" if s.get("paused") else "▶ running"
    if s.get("paused") and s.get("paused_reason"):
        paused += f" — {s['paused_reason']}"
    return (
        f"{paused}\n"
        f"projects: {s.get('projects', 0)}\n"
        f"actionable issues: {s.get('actionable_issues', 0)}"
    )


async def render_projects(rest: httpx.AsyncClient) -> str:
    rows = (await rest.get("/api/projects")).raise_for_status().json()
    if not rows:
        return "(no registered projects)"
    lines = ["🗂 Projects:"]
    for p in rows:
        tag = " ⏸" if p.get("paused") else ""
        lines.append(f"  {p['name']}{tag} → {p['repo']}")
    return "\n".join(lines)


async def render_issue(rest: httpx.AsyncClient, project: str, number: int) -> str:
    r = await rest.get(f"/api/issues/{project}/{number}")
    if r.status_code == 404:
        return f"{project}#{number}: not found"
    r.raise_for_status()
    i = r.json()
    return (
        f"{project}#{number}: {i.get('title') or '?'}\n"
        f"  state: {i.get('state_label') or '—'}\n"
        f"  priority: {i.get('priority_label') or '—'}\n"
        f"  cycle_count: {i.get('cycle_count', 0)}\n"
        f"  url: {i.get('url') or ''}"
    )


# ---- notification rendering ----


_NOTIF_HEADERS = {
    "blocked": "🚫 Auto-blocked",
    "dispatch-failed": "💥 Dispatch failed",
    "clarification": "❓ Clarification needed",
    "decompose-approval": "📋 Decompose approval",
    "quota-warning": "⚠️ Quota warning",
    "issue-completed": "✅ Issue completed",
}


def render_notification_text(n: state.NotificationRow) -> str:
    header = _NOTIF_HEADERS.get(n.kind, f"🔔 {n.kind}")
    return f"{header}\n  {n.project}#{n.issue}"


def notification_buttons(n: state.NotificationRow) -> list[list[dict[str, str]]]:
    """Inline keyboard layout per notification kind. Returns list of rows; each
    button is a dict with 'text' and 'callback_data' (or 'url')."""
    if n.kind == "blocked":
        return [[
            {"text": "Open issue", "url": f"https://github.com/issues?q=is:issue {n.project}#{n.issue}"},
            {"text": "Mute", "callback_data": encode_callback("mute", n.project, n.issue)},
        ]]
    if n.kind == "dispatch-failed":
        return [[
            {"text": "Retry", "callback_data": encode_callback("retry", n.project, n.issue)},
            {"text": "Block", "callback_data": encode_callback("block", n.project, n.issue)},
        ]]
    if n.kind == "clarification":
        return [[
            {"text": "Mute", "callback_data": encode_callback("mute", n.project, n.issue)},
        ]]
    if n.kind == "decompose-approval":
        return [[
            {"text": "Approve", "callback_data": encode_callback("approve_decompose", n.project, n.issue)},
            {"text": "Reject", "callback_data": encode_callback("reject_decompose", n.project, n.issue)},
        ]]
    return []


# ---- callback handlers ----


async def handle_callback(
    rest: httpx.AsyncClient, payload: CallbackPayload
) -> str:
    """Dispatch an inline-button click to the right REST call. Returns a short
    status string suitable for `answerCallbackQuery`."""
    a = payload.action
    p = payload.project
    n = payload.number
    if a == "retry":
        r = await rest.post(
            f"/api/issues/{p}/{n}/dispatch", json={"stage": "plan-exec"}
        )
        return "queued" if r.is_success else f"error {r.status_code}"
    if a == "block":
        r = await rest.post(
            f"/api/issues/{p}/{n}/label",
            json={"add": ["state:blocked"], "remove": []},
        )
        return "blocked" if r.is_success else f"error {r.status_code}"
    if a == "mute":
        return "muted"  # nothing further to do
    if a == "approve_pr":
        r = await rest.post(
            f"/api/prs/{p}/{n}/review", json={"action": "approve"}
        )
        return "approved" if r.is_success else f"error {r.status_code}"
    if a in ("approve_decompose", "reject_decompose"):
        # Let the user answer with a comment in the next reply; for now, just
        # post the verdict as a comment so the agent picks it up next tick.
        verdict = "approve" if a == "approve_decompose" else "reject"
        r = await rest.post(
            f"/api/issues/{p}/{n}/comment",
            json={"body": f"decompose:{verdict}"},
        )
        return verdict if r.is_success else f"error {r.status_code}"
    return f"unknown action: {a}"


async def handle_reply_to_notification(
    rest: httpx.AsyncClient,
    notif: state.NotificationRow,
    body: str,
) -> bool:
    """Post the reply text as a GH comment on the linked issue."""
    r = await rest.post(
        f"/api/issues/{notif.project}/{notif.issue}/comment",
        json={"body": body},
    )
    return r.is_success


# ---- bot wiring ----


@dataclass
class _PendingMessage:
    notification_id: int
    chat_id: int
    text: str
    buttons: list[list[dict[str, str]]]


ApplicationFactory = Callable[..., Any]


def _default_application_factory(token: str) -> Any:
    from telegram.ext import Application  # type: ignore[import-not-found]

    return Application.builder().token(token).build()


class TelegramBot:
    """Long-poll Telegram bot. Single chat_id auth.

    The Application object and the httpx client are constructed in `start()`
    so unit tests can avoid them — call the handler methods (`_cmd_queue` etc.)
    directly with a mock `context.bot`.
    """

    def __init__(
        self,
        *,
        token: str,
        chat_id: int,
        rest_base_url: str,
        rest_client: httpx.AsyncClient | None = None,
        application_factory: ApplicationFactory | None = None,
        notification_poll_interval_s: float = 5.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._rest_base_url = rest_base_url
        self._rest = rest_client
        self._owns_rest = rest_client is None
        self._application_factory = application_factory or _default_application_factory
        self._poll_interval = notification_poll_interval_s
        self._app: Any = None
        self._notif_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # ---- lifecycle ----

    async def start(self) -> None:
        if self._rest is None:
            self._rest = httpx.AsyncClient(base_url=self._rest_base_url, timeout=30.0)
        self._app = self._application_factory(self._token)
        self._register_handlers(self._app)
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        self._notif_task = asyncio.create_task(self._notification_loop())
        log.info("telegram bot started, chat_id=%s", self._chat_id)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._notif_task:
            self._notif_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._notif_task
        if self._app:
            with contextlib.suppress(Exception):
                await self._app.updater.stop()
            with contextlib.suppress(Exception):
                await self._app.stop()
            with contextlib.suppress(Exception):
                await self._app.shutdown()
        if self._owns_rest and self._rest is not None:
            await self._rest.aclose()
        log.info("telegram bot stopped")

    # ---- handler registration ----

    def _register_handlers(self, app: Any) -> None:
        from telegram.ext import (  # type: ignore[import-not-found]
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        chat_filter = filters.Chat(chat_id=self._chat_id)
        app.add_handler(CommandHandler("queue", self._cmd_queue, filters=chat_filter))
        app.add_handler(CommandHandler("status", self._cmd_status, filters=chat_filter))
        app.add_handler(CommandHandler("pause", self._cmd_pause, filters=chat_filter))
        app.add_handler(CommandHandler("resume", self._cmd_resume, filters=chat_filter))
        app.add_handler(CommandHandler("projects", self._cmd_projects, filters=chat_filter))
        app.add_handler(CommandHandler("issue", self._cmd_issue, filters=chat_filter))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        app.add_handler(
            MessageHandler(
                chat_filter & filters.REPLY & ~filters.COMMAND,
                self._on_reply,
            )
        )

    # ---- command handlers (test these directly) ----

    async def _send(self, context: Any, text: str) -> None:
        await context.bot.send_message(chat_id=self._chat_id, text=text)

    async def _cmd_queue(self, update: Any, context: Any) -> None:
        await self._send(context, await render_queue(self._rest))

    async def _cmd_status(self, update: Any, context: Any) -> None:
        await self._send(context, await render_status(self._rest))

    async def _cmd_projects(self, update: Any, context: Any) -> None:
        await self._send(context, await render_projects(self._rest))

    async def _cmd_pause(self, update: Any, context: Any) -> None:
        reason = " ".join(getattr(context, "args", []) or [])
        r = await self._rest.post("/api/pause", json={"reason": reason or None})
        text = "paused" if r.is_success else f"error {r.status_code}"
        if reason:
            text += f": {reason}"
        await self._send(context, text)

    async def _cmd_resume(self, update: Any, context: Any) -> None:
        r = await self._rest.post("/api/resume")
        await self._send(
            context, "resumed" if r.is_success else f"error {r.status_code}"
        )

    async def _cmd_issue(self, update: Any, context: Any) -> None:
        args = list(getattr(context, "args", []) or [])
        if len(args) != 2 or not args[1].isdigit():
            await self._send(context, "usage: /issue <project> <number>")
            return
        project, number = args[0], int(args[1])
        await self._send(context, await render_issue(self._rest, project, number))

    async def _on_callback(self, update: Any, context: Any) -> None:
        query = update.callback_query
        if query is None or query.message.chat.id != self._chat_id:
            return
        payload = decode_callback(query.data or "")
        if payload is None:
            await query.answer("malformed action")
            return
        result = await handle_callback(self._rest, payload)
        await query.answer(result)

    async def _on_reply(self, update: Any, context: Any) -> None:
        msg = update.message
        if msg is None or msg.reply_to_message is None:
            return
        replied = msg.reply_to_message
        notif = await asyncio.to_thread(
            state.find_notification_by_tg_message, replied.chat.id, replied.message_id
        )
        if notif is None:
            return
        ok = await handle_reply_to_notification(self._rest, notif, msg.text or "")
        await self._send(context, "comment posted" if ok else "comment failed")

    # ---- notification polling ----

    async def _notification_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._send_pending_notifications()
            except Exception:
                log.exception("notification poll failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
                return
            except asyncio.TimeoutError:
                continue

    async def _send_pending_notifications(self) -> None:
        notifs = await asyncio.to_thread(state.list_unacked_notifications)
        for n in notifs:
            text = render_notification_text(n)
            buttons = notification_buttons(n)
            try:
                msg = await self._send_notification_message(text, buttons)
            except Exception:
                log.exception("send_message failed for notification %s", n.id)
                continue
            await asyncio.to_thread(
                state.set_notification_tg_message,
                n.id,
                chat_id=msg["chat_id"],
                message_id=msg["message_id"],
            )
            await asyncio.to_thread(
                state.acknowledge_notification, n.id, delivered_to="telegram"
            )

    async def _send_notification_message(
        self, text: str, buttons: list[list[dict[str, str]]]
    ) -> dict[str, int]:
        """Adapter over the real Application's send_message. Returns (chat_id, message_id)."""
        from telegram import (  # type: ignore[import-not-found]
            InlineKeyboardButton,
            InlineKeyboardMarkup,
        )

        markup = None
        if buttons:
            rows = [
                [
                    InlineKeyboardButton(
                        b["text"],
                        callback_data=b.get("callback_data"),
                        url=b.get("url"),
                    )
                    for b in row
                ]
                for row in buttons
            ]
            markup = InlineKeyboardMarkup(rows)
        msg = await self._app.bot.send_message(
            chat_id=self._chat_id, text=text, reply_markup=markup
        )
        return {"chat_id": msg.chat.id, "message_id": msg.message_id}


__all__ = [
    "CallbackPayload",
    "TelegramBot",
    "decode_callback",
    "encode_callback",
    "handle_callback",
    "handle_reply_to_notification",
    "notification_buttons",
    "render_issue",
    "render_notification_text",
    "render_projects",
    "render_queue",
    "render_status",
]
