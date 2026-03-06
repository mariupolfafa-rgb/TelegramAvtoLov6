import asyncio
import logging
import json
import os
import re
import random
from datetime import datetime, timedelta
from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChatWriteForbiddenError, InviteHashExpiredError, InviteHashInvalidError
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest, GetDiscussionMessageRequest
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler
import nest_asyncio
nest_asyncio.apply()

# ========== НАСТРОЙКИ ==========
# Данные для пользовательского аккаунта (комментатора)
USER_API_ID = 38611409
USER_API_HASH = 'f32e667381a1ac988b8530658ffbef0b'
USER_PHONE = '+17087366241'

# Данные для управляющего БОТА
BOT_TOKEN = "8687777365:AAFeI8nIQcYUgyYp0Ol3Fwrx_pdSYRFLxKA"
ADMIN_CHAT_ID = 8558085032  # ТВОЙ ID

# Каналы для мониторинга
CHANNELS = []  # Публичные каналы
PRIVATE_CHANNELS = {}  # Приватные каналы {channel_id: invite_link}

# Настройки комментариев
COMMENT_TEXTS = [
    "я первый",
    "первый!",
    "кто первый?",
    "я здесь!",
    "топ 1"
]
COMMENT_TEXT = random.choice(COMMENT_TEXTS)

CHECK_INTERVAL = 30
MAX_CHANNELS = 50

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
is_bot_running = False
last_posts = {}
DATA_FILE = "last_posts.json"
user_client = None
joined_private_channels = set()
comment_stats = {'total': 0, 'success': 0, 'failed': 0, 'last_comment_time': None}

# Режимы ожидания
waiting_for_private = False
waiting_for_public = False
waiting_for_text = False
waiting_for_interval = False
waiting_for_remove = False

# ========== НАСТРОЙКА ЛОГОВ ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== ФУНКЦИИ РАБОТЫ С ДАННЫМИ ==========
def load_data():
    global last_posts, CHANNELS, PRIVATE_CHANNELS, COMMENT_TEXT, CHECK_INTERVAL, comment_stats, joined_private_channels
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_posts = data.get('last_posts', {})
                CHANNELS = data.get('channels', [])
                PRIVATE_CHANNELS = data.get('private_channels', {})
                joined_private_channels = set(data.get('joined_channels', []))
                COMMENT_TEXT = data.get('comment_text', COMMENT_TEXT)
                CHECK_INTERVAL = data.get('check_interval', CHECK_INTERVAL)
                comment_stats = data.get('stats', comment_stats)
            logger.info(f"📂 Загружено: {len(CHANNELS)} публичных, {len(PRIVATE_CHANNELS)} приватных")
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")

def save_data():
    try:
        data = {
            'last_posts': last_posts,
            'channels': CHANNELS,
            'private_channels': PRIVATE_CHANNELS,
            'joined_channels': list(joined_private_channels),
            'comment_text': COMMENT_TEXT,
            'check_interval': CHECK_INTERVAL,
            'stats': comment_stats,
            'saved_at': datetime.now().isoformat()
        }
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Данные сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def extract_channel_username(text):
    """Извлекает username из ссылки или текста"""
    text = text.strip()
    
    # Убираем @ в начале
    if text.startswith('@'):
        text = text[1:]
    
    # Извлекаем из URL
    patterns = [
        r'(?:https?://)?(?:www\.)?t\.me/([a-zA-Z0-9_]+)',
        r'(?:https?://)?(?:www\.)?telegram\.me/([a-zA-Z0-9_]+)',
        r'^([a-zA-Z0-9_]+)$'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            username = match.group(1)
            if username and re.match(r'^[a-zA-Z0-9_]+$', username):
                return username.lower()
    
    return None

def is_private_invite_link(text):
    text = text.strip()
    return bool(re.search(r'(?:https?://)?(?:www\.)?t\.me/\+([a-zA-Z0-9_-]+)', text)) or \
           bool(re.search(r'(?:https?://)?(?:www\.)?t\.me/joinchat/([a-zA-Z0-9_-]+)', text))

# ========== ФУНКЦИИ ДЛЯ ПОЛЬЗОВАТЕЛЬСКОГО КЛИЕНТА ==========
async def init_user_client():
    global user_client
    try:
        if user_client is None:
            user_client = TelegramClient('user_session', USER_API_ID, USER_API_HASH)
            user_client.flood_sleep_threshold = 60
            await user_client.start(phone=USER_PHONE)
            me = await user_client.get_me()
            logger.info(f"✅ Вход выполнен: {me.first_name}")
        return user_client
    except Exception as e:
        logger.error(f"❌ Ошибка подключения: {e}")
        return None

# ========== ФУНКЦИЯ ВСТУПЛЕНИЯ В ПРИВАТНЫЙ КАНАЛ ==========
async def join_private_channel(client, invite_link):
    try:
        logger.info(f"🔐 Вступление: {invite_link}")
        
        if 'joinchat/' in invite_link:
            hash_part = invite_link.split('joinchat/')[-1].split('?')[0]
        elif '+' in invite_link:
            hash_part = invite_link.split('+')[-1].split('?')[0]
        else:
            hash_part = invite_link
            
        try:
            invite = await client(CheckChatInviteRequest(hash=hash_part))
            title = getattr(invite, 'title', 'Unknown')
        except Exception as e:
            return None, f"Ошибка проверки: {e}"
        
        try:
            updates = await client(ImportChatInviteRequest(hash=hash_part))
            for chat in updates.chats:
                if hasattr(chat, 'id'):
                    channel_id = f"private_{chat.id}"
                    title = getattr(chat, 'title', 'Unknown')
                    return channel_id, title
            return None, "Не удалось получить информацию"
        except InviteHashExpiredError:
            return None, "❌ Ссылка истекла"
        except InviteHashInvalidError:
            return None, "❌ Недействительная ссылка"
        except Exception as e:
            return None, f"❌ Ошибка: {str(e)[:100]}"
    except Exception as e:
        return None, str(e)

# ========== ФУНКЦИИ КОММЕНТИРОВАНИЯ ==========
async def leave_comment(client, channel_identifier, post_id):
    global comment_stats
    try:
        if isinstance(channel_identifier, str) and channel_identifier.startswith('private_'):
            numeric_id = int(channel_identifier.replace('private_', ''))
            channel = await client.get_entity(numeric_id)
        else:
            channel = await client.get_entity(channel_identifier)
        
        post = await client.get_messages(channel, ids=int(post_id))
        if not post:
            return False
        
        comment_stats['total'] += 1
        
        try:
            await client.send_message(entity=channel, message=COMMENT_TEXT, comment_to=post.id)
            comment_stats['success'] += 1
            comment_stats['last_comment_time'] = datetime.now().isoformat()
            save_data()
            return True
        except:
            try:
                await client.send_message(channel, COMMENT_TEXT, reply_to=post.id)
                comment_stats['success'] += 1
                return True
            except:
                comment_stats['failed'] += 1
                return False
    except Exception as e:
        logger.error(f"Ошибка комментирования: {e}")
        return False

# ========== ОСНОВНОЙ ОБРАБОТЧИК ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ У вас нет прав")
        return
    
    keyboard = [
        [InlineKeyboardButton("🚀 Запустить мониторинг", callback_data='start_bot')],
        [InlineKeyboardButton("⏹ Остановить", callback_data='stop_bot')],
        [InlineKeyboardButton("📊 Статус", callback_data='status')],
        [InlineKeyboardButton("📋 Список каналов", callback_data='channels')],
        [InlineKeyboardButton("⚙️ Настройки", callback_data='settings')],
        [InlineKeyboardButton("➕ Добавить канал", callback_data='add_channel_menu')]
    ]
    await update.message.reply_text(
        "🤖 **Управление ботом-комментатором**\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок"""
    global waiting_for_private, waiting_for_public, waiting_for_text, waiting_for_interval, waiting_for_remove
    
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ У вас нет прав")
        return
    
    global is_bot_running, COMMENT_TEXT, CHECK_INTERVAL
    
    if query.data == 'start_bot':
        if is_bot_running:
            await query.edit_message_text("❌ Бот уже запущен!")
        else:
            is_bot_running = True
            await query.edit_message_text("🚀 Запускаю мониторинг...")
            asyncio.create_task(run_comment_bot(context.bot))
    
    elif query.data == 'stop_bot':
        is_bot_running = False
        await query.edit_message_text("⏹ Бот остановлен")
    
    elif query.data == 'status':
        text = f"📊 **СТАТУС**\n\n"
        text += f"🟢 Работает: {'✅' if is_bot_running else '❌'}\n"
        text += f"📝 Публичных каналов: {len(CHANNELS)}\n"
        text += f"🔐 Приватных каналов: {len(PRIVATE_CHANNELS)}\n"
        text += f"💬 Текст комментария: '{COMMENT_TEXT}'\n"
        text += f"⏱ Интервал проверки: {CHECK_INTERVAL} сек\n\n"
        text += f"📈 Статистика: {comment_stats['success']}/{comment_stats['total']}"
        await query.edit_message_text(text, parse_mode='Markdown')
    
    elif query.data == 'channels':
        text = "📋 **СПИСОК КАНАЛОВ**\n\n"
        
        text += "**📢 Публичные каналы:**\n"
        if CHANNELS:
            for i, ch in enumerate(CHANNELS, 1):
                text += f"{i}. @{ch}\n"
        else:
            text += "Нет публичных каналов\n"
        
        text += "\n**🔐 Приватные каналы:**\n"
        if PRIVATE_CHANNELS:
            for i, (ch_id, link) in enumerate(PRIVATE_CHANNELS.items(), 1):
                status = "✅" if ch_id in joined_private_channels else "⏳"
                text += f"{i}. {status} {ch_id}\n"
        else:
            text += "Нет приватных каналов\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ Добавить канал", callback_data='add_channel_menu')],
            [InlineKeyboardButton("➖ Удалить канал", callback_data='remove_channel_menu')],
            [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
        ]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == 'add_channel_menu':
        keyboard = [
            [InlineKeyboardButton("📢 Публичный канал", callback_data='add_public')],
            [InlineKeyboardButton("🔐 Приватный канал", callback_data='add_private')],
            [InlineKeyboardButton("🔙 Назад", callback_data='channels')]
        ]
        await query.edit_message_text(
            "➕ **ДОБАВЛЕНИЕ КАНАЛА**\n\nВыберите тип канала:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == 'remove_channel_menu':
        text = "➖ **УДАЛЕНИЕ КАНАЛА**\n\n"
        text += "Отправьте username публичного канала или ID приватного канала для удаления\n"
        text += "Например: @durov или private_123456789\n\n"
        text += "Или /cancel для отмены"
        
        waiting_for_remove = True
        await query.edit_message_text(text, parse_mode='Markdown')
    
    elif query.data == 'add_public':
        waiting_for_public = True
        await query.edit_message_text(
            "📢 **ДОБАВЛЕНИЕ ПУБЛИЧНОГО КАНАЛА**\n\n"
            "Отправьте username или ссылку:\n"
            "• durov\n"
            "• @durov\n"
            "• https://t.me/durov\n\n"
            "Или /cancel для отмены",
            parse_mode='Markdown'
        )
    
    elif query.data == 'add_private':
        waiting_for_private = True
        await query.edit_message_text(
            "🔐 **ДОБАВЛЕНИЕ ПРИВАТНОГО КАНАЛА**\n\n"
            "Отправьте ссылку-приглашение:\n"
            "• https://t.me/+COBtMLnnTos5YmEy\n"
            "• https://t.me/joinchat/COBtMLnnTos5YmEy\n\n"
            "Или /cancel для отмены",
            parse_mode='Markdown'
        )
    
    elif query.data == 'settings':
        keyboard = [
            [InlineKeyboardButton("✏️ Изменить текст", callback_data='change_text')],
            [InlineKeyboardButton("⏱ Изменить интервал", callback_data='change_interval')],
            [InlineKeyboardButton("🎲 Случайный текст", callback_data='random_text')],
            [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
        ]
        await query.edit_message_text(
            f"⚙️ **НАСТРОЙКИ**\n\n"
            f"Текущий текст: '{COMMENT_TEXT}'\n"
            f"Интервал проверки: {CHECK_INTERVAL} сек",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == 'random_text':
        global COMMENT_TEXTS
        COMMENT_TEXT = random.choice(COMMENT_TEXTS)
        save_data()
        await query.edit_message_text(f"✅ Случайный текст выбран: '{COMMENT_TEXT}'")
    
    elif query.data == 'change_text':
        waiting_for_text = True
        await query.edit_message_text(
            f"✏️ **ИЗМЕНЕНИЕ ТЕКСТА**\n\n"
            f"Текущий текст: '{COMMENT_TEXT}'\n\n"
            f"Отправьте новый текст (макс. 200 символов)\n"
            f"Или /cancel",
            parse_mode='Markdown'
        )
    
    elif query.data == 'change_interval':
        waiting_for_interval = True
        await query.edit_message_text(
            f"⏱ **ИЗМЕНЕНИЕ ИНТЕРВАЛА**\n\n"
            f"Текущий интервал: {CHECK_INTERVAL} сек\n\n"
            f"Введите новое значение (минимум 10, максимум 3600)\n"
            f"Или /cancel",
            parse_mode='Markdown'
        )
    
    elif query.data == 'back_to_menu':
        keyboard = [
            [InlineKeyboardButton("🚀 Запустить мониторинг", callback_data='start_bot')],
            [InlineKeyboardButton("⏹ Остановить", callback_data='stop_bot')],
            [InlineKeyboardButton("📊 Статус", callback_data='status')],
            [InlineKeyboardButton("📋 Список каналов", callback_data='channels')],
            [InlineKeyboardButton("⚙️ Настройки", callback_data='settings')],
            [InlineKeyboardButton("➕ Добавить канал", callback_data='add_channel_menu')]
        ]
        await query.edit_message_text(
            "🤖 **Управление ботом-комментатором**\n\nВыберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех сообщений"""
    global waiting_for_private, waiting_for_public, waiting_for_text, waiting_for_interval, waiting_for_remove
    global COMMENT_TEXT, CHECK_INTERVAL, CHANNELS, PRIVATE_CHANNELS, joined_private_channels
    
    text = update.message.text
    user_id = update.effective_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    logger.info(f"📨 Сообщение: {text}")
    
    # Обработка отмены
    if text == '/cancel':
        waiting_for_private = waiting_for_public = waiting_for_text = waiting_for_interval = waiting_for_remove = False
        await update.message.reply_text("❌ Действие отменено")
        return
    
    # ===== РЕЖИМ УДАЛЕНИЯ КАНАЛА =====
    if waiting_for_remove:
        removed = False
        
        # Проверяем публичные каналы
        for ch in CHANNELS[:]:
            if ch in text or f"@{ch}" in text:
                CHANNELS.remove(ch)
                removed = True
                await update.message.reply_text(f"✅ Публичный канал @{ch} удален")
                break
        
        # Проверяем приватные каналы
        for ch_id in list(PRIVATE_CHANNELS.keys()):
            if ch_id in text:
                del PRIVATE_CHANNELS[ch_id]
                if ch_id in joined_private_channels:
                    joined_private_channels.remove(ch_id)
                removed = True
                await update.message.reply_text(f"✅ Приватный канал {ch_id} удален")
                break
        
        if not removed:
            await update.message.reply_text("❌ Канал не найден")
        
        save_data()
        waiting_for_remove = False
        return
    
    # ===== РЕЖИМ ДОБАВЛЕНИЯ ПУБЛИЧНОГО КАНАЛА =====
    if waiting_for_public:
        username = extract_channel_username(text)
        
        if not username:
            await update.message.reply_text(
                "❌ Не удалось распознать канал\n\n"
                "Отправьте:\n• durov\n• @durov\n• https://t.me/durov"
            )
            return
        
        if username in CHANNELS:
            await update.message.reply_text(f"❌ Канал @{username} уже в списке")
            waiting_for_public = False
            return
        
        status = await update.message.reply_text(f"🔄 Проверяю канал @{username}...")
        
        try:
            client = await init_user_client()
            if not client:
                await status.edit_text("❌ Ошибка подключения")
                waiting_for_public = False
                return
            
            # Проверяем существование канала
            entity = await client.get_entity(username)
            title = getattr(entity, 'title', username)
            
            CHANNELS.append(username)
            save_data()
            
            await status.edit_text(
                f"✅ **Публичный канал добавлен!**\n\n"
                f"📢 Название: {title}\n"
                f"🔗 Username: @{username}",
                parse_mode='Markdown'
            )
            
        except Exception as e:
            await status.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        
        waiting_for_public = False
        return
    
    # ===== РЕЖИМ ДОБАВЛЕНИЯ ПРИВАТНОГО КАНАЛА =====
    if waiting_for_private:
        if not is_private_invite_link(text):
            await update.message.reply_text(
                "❌ Это не похоже на ссылку-приглашение\n\n"
                "Нужно: https://t.me/+COBtMLnnTos5YmEy\n"
                "Или /cancel"
            )
            return
        
        status = await update.message.reply_text("🔄 Обрабатываю ссылку...")
        
        try:
            client = await init_user_client()
            if not client:
                await status.edit_text("❌ Ошибка подключения")
                waiting_for_private = False
                return
            
            result, title = await join_private_channel(client, text)
            
            if result:
                PRIVATE_CHANNELS[result] = text
                joined_private_channels.add(result)
                save_data()
                await status.edit_text(
                    f"✅ **Приватный канал добавлен!**\n\n"
                    f"📢 Название: {title}\n"
                    f"🔐 ID: `{result}`",
                    parse_mode='Markdown'
                )
            else:
                await status.edit_text(f"❌ {title}")
                return
                
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        
        waiting_for_private = False
        return
    
    # ===== РЕЖИМ ИЗМЕНЕНИЯ ТЕКСТА =====
    if waiting_for_text:
        if len(text) > 200:
            await update.message.reply_text("❌ Слишком длинный (макс. 200 символов)")
            return
        COMMENT_TEXT = text
        save_data()
        await update.message.reply_text(f"✅ Текст изменен: '{COMMENT_TEXT}'")
        waiting_for_text = False
        return
    
    # ===== РЕЖИМ ИЗМЕНЕНИЯ ИНТЕРВАЛА =====
    if waiting_for_interval:
        try:
            interval = int(text)
            if interval < 10:
                await update.message.reply_text("❌ Минимум 10 секунд")
                return
            if interval > 3600:
                await update.message.reply_text("❌ Максимум 3600 секунд")
                return
            CHECK_INTERVAL = interval
            save_data()
            await update.message.reply_text(f"✅ Интервал изменен на {CHECK_INTERVAL} сек")
            waiting_for_interval = False
        except ValueError:
            await update.message.reply_text("❌ Введите число")
        return
    
    # Если не в режиме - показываем меню
    await update.message.reply_text("Используйте /start для управления ботом")

# ========== ЗАПУСК МОНИТОРИНГА ==========
async def monitor_channels(client, bot):
    global is_bot_running, last_posts
    
    while is_bot_running:
        try:
            # Мониторинг публичных каналов
            for channel in CHANNELS:
                if not is_bot_running:
                    break
                try:
                    channel_entity = await client.get_entity(channel)
                    messages = await client.get_messages(channel_entity, limit=1)
                    if messages:
                        post_id = str(messages[0].id)
                        key = f"public_{channel}"
                        
                        if key not in last_posts:
                            last_posts[key] = post_id
                            save_data()
                        elif last_posts[key] != post_id:
                            logger.info(f"🎯 Новый пост в @{channel}")
                            success = await leave_comment(client, channel, post_id)
                            if success:
                                last_posts[key] = post_id
                                save_data()
                                await bot.send_message(
                                    chat_id=ADMIN_CHAT_ID,
                                    text=f"💬 **Прокомментировано!**\n📢 Канал: @{channel}",
                                    parse_mode='Markdown'
                                )
                except Exception as e:
                    logger.error(f"Ошибка {channel}: {e}")
                await asyncio.sleep(5)
            
            # Мониторинг приватных каналов
            for channel_id in PRIVATE_CHANNELS:
                if not is_bot_running:
                    break
                try:
                    if channel_id not in joined_private_channels:
                        continue
                    
                    numeric_id = int(channel_id.replace('private_', ''))
                    channel_entity = await client.get_entity(numeric_id)
                    messages = await client.get_messages(channel_entity, limit=1)
                    
                    if messages:
                        post_id = str(messages[0].id)
                        key = f"private_{channel_id}"
                        
                        if key not in last_posts:
                            last_posts[key] = post_id
                            save_data()
                        elif last_posts[key] != post_id:
                            logger.info(f"🎯 Новый пост в приватном канале")
                            success = await leave_comment(client, channel_id, post_id)
                            if success:
                                last_posts[key] = post_id
                                save_data()
                                await bot.send_message(
                                    chat_id=ADMIN_CHAT_ID,
                                    text=f"💬 **Прокомментировано в приватном канале!**",
                                    parse_mode='Markdown'
                                )
                except Exception as e:
                    logger.error(f"Ошибка приватного: {e}")
                await asyncio.sleep(5)
            
            if is_bot_running:
                logger.info(f"💤 Ожидание {CHECK_INTERVAL} сек...")
                await asyncio.sleep(CHECK_INTERVAL)
                
        except Exception as e:
            logger.error(f"Ошибка в мониторинге: {e}")
            await asyncio.sleep(60)

async def run_comment_bot(bot):
    global user_client, is_bot_running
    try:
        client = await init_user_client()
        if client:
            total = len(CHANNELS) + len(PRIVATE_CHANNELS)
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🚀 **Мониторинг запущен!**\n\nОтслеживается каналов: {total}",
                parse_mode='Markdown'
            )
            await monitor_channels(client, bot)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
    finally:
        is_bot_running = False

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========
async def main():
    load_data()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    try:
        await app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="🤖 **Бот запущен!**\nИспользуйте /start для управления",
            parse_mode='Markdown'
        )
    except:
        pass
    
    logger.info("✅ Бот запущен")
    
    try:
        while True:
            await asyncio.sleep(3600)
            save_data()
    except:
        pass
    finally:
        global is_bot_running
        is_bot_running = False
        save_data()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if user_client:
            await user_client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
