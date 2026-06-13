"""
TrackingMixin — group and user registration pipeline methods.
"""

import logging
import time
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.error import Forbidden, BadRequest
from telegram.ext import ContextTypes

from src.bot.ui import UI, get_thread_id

logger = logging.getLogger(__name__)


class TrackingMixin(object):

    # ══════════════════════════════════════════════════════════════
    #  GROUP TRACKING — SINGLE PIPELINE
    # ══════════════════════════════════════════════════════════════

    def ensure_group_registered(
        self,
        update: Update,
        context=None,
        source: str = "unknown",
    ) -> None:
        """Central group registration pipeline — call from every handler."""
        if not self.db:
            return

        chat = update.effective_chat

        # poll_answer has no effective_chat — recover chat_id from bot_data
        if chat is None and context is not None and update.poll_answer:
            poll_id  = update.poll_answer.poll_id
            data     = context.bot_data.get(f"poll_{poll_id}", {})
            chat_id  = data.get("chat_id")
            if chat_id and isinstance(chat_id, int) and chat_id < 0:
                if chat_id not in self._seen_groups:
                    try:
                        self.db.register_group_interaction(
                            chat_id  = chat_id,
                            title    = data.get("chat_title", ""),
                            username = ""
                        )
                        self._seen_groups.add(chat_id)
                        logger.info(
                            f"[GROUP REGISTERED] id={chat_id} source={source}"
                        )
                    except Exception as e:
                        logger.error(
                            f"ensure_group_registered poll {chat_id}: {e}"
                        )
            return

        if not chat or chat.type not in ("group", "supergroup"):
            return

        if chat.id in self._seen_groups:
            return  # already upserted this session — skip redundant DB write

        try:
            self.db.register_group_interaction(
                chat_id  = chat.id,
                thread_id= get_thread_id(update),
                title    = chat.title or "",
                username = getattr(chat, "username", "") or ""
            )
            self._seen_groups.add(chat.id)
            logger.info(
                f"[GROUP REGISTERED] id={chat.id} title={chat.title!r} "
                f"source={source}"
            )
        except Exception as e:
            logger.error(
                f"ensure_group_registered {chat.id} ({source}): {e}"
            )

    # ══════════════════════════════════════════════════════════════
    #  USER TRACKING — SINGLE PIPELINE
    # ══════════════════════════════════════════════════════════════

    _USER_CACHE_TTL = 300  # seconds

    def ensure_user_registered(
        self,
        user,
        is_pm: bool = None,
        source: str = "unknown",
    ) -> None:
        """Central user registration pipeline."""
        if not self.db or not user:
            return

        now_ts = time.time()
        cached = self._seen_users.get(user.id)
        if cached and (now_ts - cached) < self._USER_CACHE_TTL:
            return  # already upserted within TTL — skip redundant write

        try:
            data: dict = {
                "user_id":   user.id,
                "username":  user.username or "",
                "name":      UI.display_name(user),
                "last_seen": datetime.utcnow().isoformat(),
                "active_status": "active",
            }
            if is_pm is not None:
                data["pm_accessible"] = is_pm
            self.db.upsert_user(user.id, data)
            self._seen_users[user.id] = now_ts
            logger.debug(f"[USER REGISTERED] id={user.id} source={source}")
        except Exception as e:
            logger.error(f"ensure_user_registered {user.id} ({source}): {e}")

    # ─── Bot membership change handler ───────────────────────

    async def handle_my_chat_member(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Full lifecycle: add / remove / promote / demote / restrict / rejoin."""
        member = update.my_chat_member
        if not member:
            return
        chat = member.chat
        if chat.type not in ("group", "supergroup"):
            return

        new_status = member.new_chat_member.status
        old_status = member.old_chat_member.status

        if new_status in ("member", "administrator", "restricted"):
            # Bot present (active or restricted) — register / refresh metadata
            self.ensure_group_registered(update, context, source="my_chat_member")

            # Track admin status changes
            if self.db:
                try:
                    is_admin = (new_status == "administrator")
                    perms = None
                    if is_admin:
                        cm = member.new_chat_member
                        perms = {
                            "can_delete_messages":  getattr(cm, "can_delete_messages",  False),
                            "can_restrict_members": getattr(cm, "can_restrict_members", False),
                            "can_pin_messages":     getattr(cm, "can_pin_messages",     False),
                            "can_manage_chat":      getattr(cm, "can_manage_chat",      False),
                        }
                    self.db.update_group_admin_status(chat.id, is_admin, perms)
                except Exception as e:
                    logger.error(f"update_group_admin_status {chat.id}: {e}")

            action = {
                "member":        "added",
                "administrator": "promoted",
                "restricted":    "restricted",
            }.get(new_status, new_status)
            if old_status in ("left", "kicked", "banned"):
                logger.info(
                    f"[GROUP REACTIVATED] id={chat.id} title={chat.title!r}"
                )
            else:
                logger.info(
                    f"[GROUP {action.upper()}] id={chat.id} title={chat.title!r}"
                )

        elif new_status in ("left", "kicked", "banned"):
            # Bot removed — purge from DB so counts stay accurate
            if self.db:
                try:
                    self.db.remove_inactive_group(chat.id)
                    self._seen_groups.discard(chat.id)
                    logger.info(
                        f"[GROUP REMOVED] id={chat.id} title={chat.title!r} "
                        f"status={new_status}"
                    )
                except Exception as e:
                    logger.error(
                        f"handle_my_chat_member remove {chat.id}: {e}"
                    )

    async def _handle_group_migration(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Group → supergroup migration: move DB record to new chat_id."""
        msg  = update.effective_message
        chat = update.effective_chat
        if not msg or not self.db or not chat:
            return

        new_id = getattr(msg, "migrate_to_chat_id", None)
        old_id = getattr(msg, "migrate_from_chat_id", None)

        if new_id:
            # Message from the OLD group chat — transfer record to new ID
            try:
                self.db.remove_inactive_group(chat.id)
                self._seen_groups.discard(chat.id)
            except Exception as e:
                logger.error(f"migration remove old {chat.id}: {e}")
            # Register new supergroup ID using same title/username
            try:
                self.db.register_group_interaction(
                    chat_id  = new_id,
                    title    = chat.title or "",
                    username = getattr(chat, "username", "") or ""
                )
                self._seen_groups.add(new_id)
                logger.info(
                    f"[GROUP MIGRATION] old={chat.id} → new={new_id} "
                    f"title={chat.title!r}"
                )
            except Exception as e:
                logger.error(f"migration register new {new_id}: {e}")

        elif old_id:
            # Message from the NEW supergroup — ensure it is registered
            self.ensure_group_registered(update, context, source="migration-new-id")

    async def _auto_track(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handler group 1 — fires alongside every group message.
        Registers both the group and the sending user in a single pass."""
        self.ensure_group_registered(update, context, source="passive-message")
        user = update.effective_user
        if user:
            chat = update.effective_chat
            is_pm = chat.type == "private" if chat else False
            self.ensure_user_registered(user, is_pm=is_pm if is_pm else None,
                                        source="passive-message")

    async def recover_groups_from_history(self) -> None:
        """Startup recovery: find every group chat_id in activity history that is
        not yet in the groups collection, call getChat() for each, and register it."""
        if not self.db or not self.application:
            return
        try:
            known_ids   = self.db.get_known_group_ids_from_history()
            registered  = self.db.get_registered_group_ids()
            missing_ids = known_ids - registered

            if not missing_ids:
                logger.info("[STARTUP RECOVERY] No unregistered groups found in history")
                return

            logger.info(
                f"[STARTUP RECOVERY] {len(missing_ids)} group IDs in history but "
                f"not in DB — fetching metadata from Telegram API"
            )

            recovered = skipped = failed = 0
            for chat_id in missing_ids:
                try:
                    chat = await self.application.bot.get_chat(chat_id)
                    if chat.type in ("group", "supergroup"):
                        self.db.register_group_interaction(
                            chat_id  = chat.id,
                            title    = chat.title or "",
                            username = getattr(chat, "username", "") or ""
                        )
                        self._seen_groups.add(chat.id)
                        logger.info(
                            f"[GROUP RECOVERED] id={chat_id} title={chat.title!r}"
                        )
                        recovered += 1
                    else:
                        skipped += 1
                except Forbidden:
                    logger.info(
                        f"[GROUP SKIP] id={chat_id} — bot no longer a member"
                    )
                    skipped += 1
                except BadRequest as e:
                    logger.warning(f"[GROUP SKIP] id={chat_id} — {e}")
                    skipped += 1
                except Exception as e:
                    logger.error(f"[GROUP RECOVER FAIL] id={chat_id}: {e}")
                    failed += 1

            logger.info(
                f"[STARTUP RECOVERY] Done — recovered={recovered} "
                f"skipped={skipped} failed={failed}"
            )
        except Exception as e:
            logger.error(f"recover_groups_from_history: {e}")
