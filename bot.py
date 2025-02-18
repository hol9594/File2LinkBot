#!/usr/bin/env python3
import os
import asyncio
import aiohttp
import logging
import threading
import time
import tempfile
import redis
import telebot
from telebot import types

# Configuración básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FileLinkBot")

class FileLinkBot:
    def __init__(self, bot, redis_conn, storage_chat_id):
        """
        :param bot: Instancia de telebot.TeleBot.
        :param redis_conn: Conexión a Redis para cachear file_ids.
        :param storage_chat_id: Chat ID donde se subirán los archivos para obtener file_id permanente.
                                  Si no se configura, se usará el chat del usuario.
        """
        self.bot = bot
        self.redis = redis_conn
        self.storage_chat_id = storage_chat_id
        self.download_queue = asyncio.Queue()  # Para futuros desarrollos (descargas con prioridad)
        self.progress_tracker = {}  # user_id -> progreso (0-100)

    async def process_url(self, url: str, user_id: int) -> str:
        """
        Descarga en streaming el archivo de la URL en chunks de 1MB,
        actualizando el progreso en self.progress_tracker.
        Devuelve la ruta del archivo descargado.
        """
        self.progress_tracker[user_id] = 0
        file_name = url.split("/")[-1] or f"file_{user_id}"
        file_path = os.path.join(tempfile.gettempdir(), f"{user_id}_{file_name}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                total = int(response.headers.get('Content-Length', 0))
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB
                with open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            progress = int((downloaded / total) * 100)
                            # Actualiza cada 10% de avance
                            if progress - self.progress_tracker[user_id] >= 10:
                                self.progress_tracker[user_id] = progress
                        else:
                            # Si no se conoce el tamaño, incrementa de forma arbitraria
                            self.progress_tracker[user_id] += 1
        self.progress_tracker[user_id] = 100
        return file_path

    async def generate_telegram_link(self, file_path: str, user_chat_id: int) -> str:
        """
        Sube el archivo a Telegram usando send_document y retorna el file_id.
        Se utiliza Redis para cachear el file_id y evitar re-subidas.
        :param file_path: Ruta local del archivo.
        :param user_chat_id: Chat ID del usuario que solicita la operación.
        """
        file_key = f"file_{os.path.basename(file_path)}"
        cached_file_id = self.redis.get(file_key)
        if cached_file_id:
            return cached_file_id.decode()

        # Si se configuró un STORAGE_CHAT_ID, se usa ese canal; de lo contrario, el chat del usuario.
        target_chat_id = self.storage_chat_id if self.storage_chat_id and self.storage_chat_id != 0 else user_chat_id

        def upload():
            with open(file_path, 'rb') as f:
                return self.bot.send_document(target_chat_id, f)
        try:
            result = await asyncio.to_thread(upload)
            file_id = result.document.file_id
            self.redis.set(file_key, file_id)
            return file_id
        except Exception as e:
            logger.error("Error en generate_telegram_link: %s", e)
            return ""

    def handle_error(self, error):
        """Registra errores (se puede extender para notificar a Sentry o a administradores)."""
        logger.error("Ocurrió un error: %s", error)
        # Aquí se puede implementar una dead-letter queue o notificar a admin vía Telegram.

# ----------------------------
# Configuración e inicialización
# ----------------------------
# Variables de entorno:
# - BOT_TOKEN: token de tu bot de Telegram
# - REDIS_URL: (opcional) URL de conexión a Redis (default: redis://localhost:6379)
# - STORAGE_CHAT_ID: (opcional) Chat ID de un canal/grupo donde se almacenarán los archivos para obtener file_ids
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
storage_chat_id_env = os.environ.get("STORAGE_CHAT_ID")
try:
    STORAGE_CHAT_ID = int(storage_chat_id_env) if storage_chat_id_env else 0
except ValueError:
    STORAGE_CHAT_ID = 0

if not BOT_TOKEN:
    logger.error("No se encontró BOT_TOKEN en las variables de entorno.")
    exit(1)

# Conexión a Redis
redis_conn = redis.Redis.from_url(REDIS_URL)

# Instancia de telebot
bot = telebot.TeleBot(BOT_TOKEN)

# Instancia de nuestro FileLinkBot
fl_bot = FileLinkBot(bot, redis_conn, STORAGE_CHAT_ID)

# ----------------------------
# Función para actualizar el progreso
# ----------------------------
def progress_updater(user_id: int, chat_id: int, message_id: int):
    """
    Actualiza cada 5 segundos el mensaje con el progreso de descarga.
    Cuando el progreso llegue al 100%, se edita el mensaje final.
    """
    while fl_bot.progress_tracker.get(user_id, 0) < 100:
        progress = fl_bot.progress_tracker.get(user_id, 0)
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                  text=f"🔄 Descargando ({progress}%)")
        except Exception as e:
            if "message is not modified" in str(e):
                pass  # Se ignora este error
            else:
                logger.error("Error actualizando progreso: %s", e)
        time.sleep(5)
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                              text="✅ Descarga completada.")
    except Exception as e:
        if "message is not modified" in str(e):
            pass
        else:
            logger.error("Error actualizando mensaje final: %s", e)

# ----------------------------
# Handlers de comandos de Telegram
# ----------------------------
@bot.message_handler(commands=['start'])
def start_handler(message):
    welcome_text = (
        "¡Bienvenido al File2Link Bot!\n\n"
        "Utiliza el comando /process <URL> para convertir una URL en un enlace de descarga.\n"
        "Ejemplo: /process https://ejemplo.com/archivo.mp4\n\n"
        "Para más ayuda, usa /help."
    )
    bot.send_message(message.chat.id, welcome_text)

@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        "Comandos disponibles:\n"
        "/start - Inicia el bot y muestra este mensaje\n"
        "/process <URL> - Procesa la URL y genera un enlace de descarga\n"
        "/get_file <file_id> - Descarga el archivo usando su file_id\n"
    )
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=['process'])
def process_handler(message):
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(message.chat.id,
                             "Por favor, proporciona una URL.\nEjemplo: /process https://ejemplo.com/archivo.mp4")
            return
        url = parts[1].strip()
        # Envía mensaje inicial de progreso
        progress_message = bot.send_message(message.chat.id, "🔄 Descargando (0%)")
        # Inicia hilo para actualizar el progreso
        progress_thread = threading.Thread(target=progress_updater,
                                           args=(message.chat.id, message.chat.id, progress_message.message_id))
        progress_thread.start()

        # Función que procesa la URL en un hilo separado y con asyncio
        def process_thread():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                file_path = loop.run_until_complete(fl_bot.process_url(url, message.chat.id))
                file_id = loop.run_until_complete(fl_bot.generate_telegram_link(file_path, message.chat.id))
                # Elimina el archivo local después de enviarlo a Telegram
                try:
                    os.remove(file_path)
                    logger.info("Archivo temporal eliminado: %s", file_path)
                except Exception as e:
                    logger.error("Error al eliminar el archivo: %s", e)
                bot.send_message(message.chat.id,
                                 f"✅ Archivo listo.\nUsa /get_file {file_id} para descargarlo.")
            except Exception as e:
                fl_bot.handle_error(e)
                bot.send_message(message.chat.id, "❌ Ocurrió un error al procesar la URL.")
        threading.Thread(target=process_thread).start()
    except Exception as e:
        fl_bot.handle_error(e)
        bot.send_message(message.chat.id, "❌ Error en el comando /process.")

@bot.message_handler(commands=['get_file'])
def get_file_handler(message):
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(message.chat.id,
                             "Por favor, proporciona el file_id.\nEjemplo: /get_file <file_id>")
            return
        file_id = parts[1].strip()
        bot.send_document(message.chat.id, file_id)
    except Exception as e:
        fl_bot.handle_error(e)
        bot.send_message(message.chat.id, "❌ Error al enviar el archivo.")

# ----------------------------
# Inicio del bot
# ----------------------------
if __name__ == '__main__':
    logger.info("Bot iniciado.")
    bot.polling(none_stop=True)
