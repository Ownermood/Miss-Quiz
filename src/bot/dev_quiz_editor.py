"""
dev_quiz_editor.py — QuizEditorMixin

Provides /editquiz and all supporting callback/text-input handlers
for the DeveloperCommands class.  All methods expect self.db, self.quiz_manager,
self.check_access(), self.send_unauthorized_message(), and self.auto_clean_message()
to be available via the MRO (set by DeveloperCommands).
"""

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from src.bot.ui import UI

logger = logging.getLogger(__name__)


class QuizEditorMixin:
    """Interactive quiz editor command handlers."""

    async def editquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Interactive quiz editor with inline keyboards (Developer only)."""
        if not update.effective_user or not update.effective_message:
            return

        import time
        start_time = time.time()

        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            # If replying to a quiz message, jump directly to that quiz's editor
            if update.message and update.message.reply_to_message:
                quiz_id = self.extract_quiz_id_from_message(update.message.reply_to_message, context)

                if quiz_id:
                    import asyncio as _asyncio
                    quiz = await _asyncio.to_thread(self.db.get_question_by_id, quiz_id)
                    if quiz:
                        logger.info(f"Editing quiz #{quiz_id} via reply")
                        await self._show_quiz_editor(update, context, quiz_id)

                        response_time = int((time.time() - start_time) * 1000)
                        await _asyncio.to_thread(self.db.log_activity,
                            activity_type='command',
                            user_id=update.effective_user.id,
                            chat_id=update.effective_message.chat_id,
                            username=update.effective_user.username or "",
                            command='/editquiz',
                            details={'quiz_id': quiz_id, 'via_reply': True},
                            success=True,
                            response_time_ms=response_time
                        )
                        return
                    else:
                        reply = await update.message.reply_text(
                            f"❌ Quiz #{quiz_id} not found in database."
                        )
                        await self.auto_clean_message(update.message, reply)
                        return
                else:
                    reply = await update.message.reply_text(
                        "❌ Could not find quiz ID in the replied message.\n\n"
                        "💡 Reply to a quiz poll to edit it,\n"
                        "or use /editquiz to browse all quizzes."
                    )
                    await self.auto_clean_message(update.message, reply)
                    return

            # Show quiz list or jump to a specific quiz by ID
            args = context.args if context.args else []

            if len(args) > 0 and args[0].isdigit():
                quiz_id = int(args[0])
                await self._show_quiz_editor(update, context, quiz_id)
            else:
                page = int(args[0]) if len(args) > 0 and args[0].isdigit() else 1
                await self._show_quiz_list(update, context, page)

            response_time = int((time.time() - start_time) * 1000)
            import asyncio as _asyncio
            await _asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_message.chat_id,
                username=update.effective_user.username or "",
                command='/editquiz',
                success=True,
                response_time_ms=response_time,
            )

        except Exception as e:
            logger.error(f"Error in editquiz: {e}", exc_info=True)
            if update.effective_message:
                await update.effective_message.reply_text(
                    "❌ Error loading quiz editor. Please try again.",
                    parse_mode=ParseMode.MARKDOWN
                )

    async def _show_quiz_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1) -> None:
        """Show paginated quiz list with selection buttons."""
        import asyncio as _asyncio
        questions = await _asyncio.to_thread(self.db.get_all_questions)

        if not questions:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "📭 No quizzes found.\n\nAdd new quizzes using /addquiz command.",
                    parse_mode=ParseMode.MARKDOWN
                )
            return

        per_page = 10
        total_pages = (len(questions) + per_page - 1) // per_page
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * per_page
        end_idx = min(start_idx + per_page, len(questions))

        text = f"""🔍 **Select Quiz to Edit**
━━━━━━━━━━━━━━━━━━━━━

📊 Total: {len(questions)} quizzes
📄 Page {page}/{total_pages}

"""

        keyboard = []
        for i in range(start_idx, end_idx):
            q = questions[i]
            category = q.get('category', 'N/A')
            question_preview = q['question'][:50] + '...' if len(q['question']) > 50 else q['question']
            text += f"{i + 1}. {question_preview}\n   📂 Category: {category or 'Uncategorized'}\n\n"
            keyboard.append([InlineKeyboardButton(
                f"✏️ Edit #{q['id']}: {question_preview[:30]}...",
                callback_data=f"edit_quiz_select_{q['id']}"
            )])

        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"edit_quiz_list_{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"edit_quiz_list_{page+1}"))
        nav_buttons.append(InlineKeyboardButton("❌ Cancel", callback_data="edit_quiz_cancel"))

        if nav_buttons:
            keyboard.append(nav_buttons)

        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        else:
            if update.effective_message:
                await update.effective_message.reply_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )

    async def _show_quiz_editor(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Show quiz editor interface with current values."""
        import asyncio as _asyncio
        quiz = await _asyncio.to_thread(self.db.get_question_by_id, quiz_id)

        if not quiz:
            error_text = f"""❌ **Quiz Not Found**

Quiz ID #{quiz_id} doesn't exist.

💡 Use /totalquiz to see all available quizzes."""

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    error_text,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        error_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
            return

        if context.user_data is not None:
            context.user_data[f'editing_quiz_{quiz_id}'] = {
                'id': quiz['id'],
                'question': quiz['question'],
                'options': quiz['options'],
                'correct_answer': quiz['correct_answer'],
                'category': quiz.get('category'),
                'original': quiz.copy()
            }

        text = self._format_quiz_editor(quiz)
        keyboard = [
            [
                InlineKeyboardButton("✏️ Edit Question", callback_data=f"edit_quiz_question_{quiz_id}"),
                InlineKeyboardButton("📝 Edit Options", callback_data=f"edit_quiz_options_{quiz_id}")
            ],
            [
                InlineKeyboardButton("📂 Change Category", callback_data=f"edit_quiz_category_{quiz_id}"),
                InlineKeyboardButton("✅ Change Answer", callback_data=f"edit_quiz_answer_{quiz_id}")
            ],
            [
                InlineKeyboardButton("💾 Save Changes", callback_data=f"edit_quiz_save_{quiz_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="edit_quiz_cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        else:
            if update.effective_message:
                await update.effective_message.reply_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )

    def _format_quiz_editor(self, quiz: dict) -> str:
        """Format quiz data for editor display."""
        options_text = ""
        for i, opt in enumerate(quiz['options']):
            marker = "✓" if i == quiz['correct_answer'] else "○"
            letter = chr(65 + i)
            options_text += f"{letter}) {opt} {marker}\n"

        category = quiz.get('category') or 'Uncategorized'
        correct_letter = chr(65 + quiz['correct_answer'])

        return f"""✏️ **Edit Quiz #{quiz['id']}**
━━━━━━━━━━━━━━━━━━━━━

**Current Question:**
{quiz['question']}

**Options:**
{options_text}
**📂 Category:** {category}
**✅ Correct Answer:** {correct_letter}

━━━━━━━━━━━━━━━━━━━━━
Select what to edit:"""

    async def handle_edit_quiz_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle all edit quiz callback queries."""
        if not update.callback_query or not update.effective_user:
            return

        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass

        data = query.data
        if not data:
            return

        try:
            await self._dispatch_edit_quiz_callback(update, context, query, data)
        except Exception as e:
            logger.error(
                f"[EDIT_QUIZ_CB] Error handling data={data!r} "
                f"user={update.effective_user.id}: {e}",
                exc_info=True,
            )
            try:
                await query.edit_message_text(
                    "❌ An error occurred. Please try /editquiz again."
                )
            except Exception:
                pass

    async def _dispatch_edit_quiz_callback(self, update, context, query, data) -> None:
        """Inner dispatch for edit_quiz callbacks."""
        if not await self.check_access(update):
            await query.edit_message_text("❌ Unauthorized access.")
            return

        if data == "edit_quiz_cancel":
            await query.edit_message_text("✅ Quiz editing cancelled.")
            return

        if data.startswith("edit_quiz_list_"):
            page = int(data.split("_")[-1])
            await self._show_quiz_list(update, context, page)

        elif data.startswith("edit_quiz_select_"):
            quiz_id = int(data.split("_")[-1])
            await self._show_quiz_editor(update, context, quiz_id)

        elif data.startswith("edit_quiz_question_"):
            quiz_id = int(data.split("_")[-1])
            if context.user_data is not None:
                context.user_data['waiting_for'] = f'quiz_question_{quiz_id}'
            await query.edit_message_text(
                f"✏️ **Edit Question**\n\nPlease send the new question text:",
                parse_mode=ParseMode.MARKDOWN
            )

        elif data.startswith("edit_quiz_options_"):
            quiz_id = int(data.split("_")[-1])
            if context.user_data is not None:
                context.user_data['waiting_for'] = f'quiz_options_{quiz_id}'
            await query.edit_message_text(
                f"""📝 **Edit Options**

Please send options in this format:
`Option1|Option2|Option3|Option4`

Example:
`Paris|London|Berlin|Rome`""",
                parse_mode=ParseMode.MARKDOWN
            )

        elif data.startswith("edit_quiz_category_"):
            quiz_id = int(data.split("_")[-1])
            await self._show_category_selector(update, context, quiz_id)

        elif data.startswith("edit_quiz_answer_"):
            quiz_id = int(data.split("_")[-1])
            await self._show_answer_selector(update, context, quiz_id)

        elif data.startswith("edit_quiz_set_category_"):
            parts = data.split("_")
            quiz_id = int(parts[4])
            category = "_".join(parts[5:])
            if category == "none":
                category = None
            else:
                # Reverse the encoding applied in _show_category_selector:
                # spaces → underscores, & → "and"
                category = category.replace("_", " ").replace(" and ", " & ")

            if context.user_data is not None:
                quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                if quiz_data:
                    quiz_data['category'] = category
                    await self._show_quiz_editor(update, context, quiz_id)

        elif data.startswith("edit_quiz_set_answer_"):
            parts = data.split("_")
            quiz_id = int(parts[4])
            answer_idx = int(parts[5])

            if context.user_data is not None:
                quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                if quiz_data:
                    quiz_data['correct_answer'] = answer_idx
                    await self._show_quiz_editor(update, context, quiz_id)

        elif data.startswith("edit_quiz_save_"):
            quiz_id = int(data.split("_")[-1])
            await self._save_quiz_changes(update, context, quiz_id)

    async def _show_category_selector(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Show category selection keyboard using the canonical QUIZ_CATEGORIES list."""
        keyboard = []
        row = []
        for name, emoji in UI.QUIZ_CATEGORIES:
            cat_key = name.replace(" ", "_").replace("&", "and")
            label = f"{emoji}  {name}"
            row.append(InlineKeyboardButton(label, callback_data=f"edit_quiz_set_category_{quiz_id}_{cat_key}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton("❌ No Category", callback_data=f"edit_quiz_set_category_{quiz_id}_none"),
            InlineKeyboardButton("🔙 Back", callback_data=f"edit_quiz_select_{quiz_id}")
        ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "📂 **Select Category**\n\nChoose a category for this quiz:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )

    async def _show_answer_selector(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Show answer selection keyboard."""
        if context.user_data is None:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return

        quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
        if not quiz_data:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return

        keyboard = []
        for i, opt in enumerate(quiz_data['options']):
            letter = chr(65 + i)
            current = "✓" if i == quiz_data['correct_answer'] else ""
            keyboard.append([InlineKeyboardButton(
                f"{letter}) {opt} {current}",
                callback_data=f"edit_quiz_set_answer_{quiz_id}_{i}"
            )])

        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"edit_quiz_select_{quiz_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "✅ **Select Correct Answer**\n\nChoose the correct option:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )

    async def _save_quiz_changes(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Save changes to quiz in database."""
        if context.user_data is None:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return

        quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
        if not quiz_data:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return

        try:
            import asyncio as _asyncio
            success = await _asyncio.to_thread(
                self.db.update_question,
                qid=quiz_id,
                question=quiz_data['question'],
                options=quiz_data['options'],
                correct_answer=quiz_data['correct_answer'],
                category=quiz_data.get('category'),
            )

            if success:
                changes = []
                original = quiz_data['original']
                if original['question'] != quiz_data['question']:
                    changes.append('question')
                if original['options'] != quiz_data['options']:
                    changes.append('options')
                if original['correct_answer'] != quiz_data['correct_answer']:
                    changes.append('correct_answer')
                if original.get('category') != quiz_data.get('category'):
                    changes.append('category')

                if update.effective_user and update.callback_query and update.callback_query.message:
                    chat_id = getattr(update.callback_query.message, 'chat_id', None)
                    if chat_id:
                        await _asyncio.to_thread(
                            self.db.log_activity,
                            activity_type='quiz_edited',
                            user_id=update.effective_user.id,
                            chat_id=chat_id,
                            username=update.effective_user.username or "",
                            details={'quiz_id': quiz_id, 'changes': changes},
                            success=True,
                        )

                text = self._format_quiz_editor(quiz_data)
                text = text.replace(
                    "Select what to edit:",
                    f"✅ **Changes Saved Successfully!**\n\nModified: {', '.join(changes)}"
                )

                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text,
                        parse_mode=ParseMode.MARKDOWN
                    )

                if context.user_data is not None:
                    del context.user_data[f'editing_quiz_{quiz_id}']
            else:
                if update.callback_query:
                    await update.callback_query.edit_message_text("❌ Failed to save changes. Quiz not found.")

        except Exception as e:
            logger.error(f"Error saving quiz changes: {e}")
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Error saving changes. Please try again.")

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text input for quiz editing."""
        if not update.message or not update.effective_user or not update.message.text:
            return

        if context.user_data is None:
            return

        waiting_for = context.user_data.get('waiting_for')
        if not waiting_for:
            return

        try:
            text = update.message.text.strip()

            if waiting_for.startswith('quiz_question_'):
                quiz_id = int(waiting_for.split('_')[-1])
                if context.user_data is not None:
                    quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                    if quiz_data:
                        quiz_data['question'] = text
                        context.user_data.pop('waiting_for', None)

                        await update.message.reply_text(
                            f"✅ Question updated!\n\nUse /editquiz {quiz_id} to continue editing.",
                            parse_mode=ParseMode.MARKDOWN
                        )

            elif waiting_for.startswith('quiz_options_'):
                quiz_id = int(waiting_for.split('_')[-1])
                if context.user_data is not None:
                    quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                    if quiz_data:
                        options = [opt.strip() for opt in text.split('|')]
                        if len(options) != 4:
                            await update.message.reply_text(
                                "❌ Invalid format. Please provide exactly 4 options separated by |",
                                parse_mode=ParseMode.MARKDOWN
                            )
                            return

                        quiz_data['options'] = options
                        context.user_data.pop('waiting_for', None)

                        await update.message.reply_text(
                            f"✅ Options updated!\n\nUse /editquiz {quiz_id} to continue editing.",
                            parse_mode=ParseMode.MARKDOWN
                        )

        except Exception as e:
            logger.error(
                f"[TEXT_INPUT] Error for user={update.effective_user.id} "
                f"waiting_for={waiting_for!r}: {e}",
                exc_info=True,
            )
            try:
                await update.message.reply_text(
                    "❌ Error processing your input. Please try again."
                )
            except Exception:
                pass
