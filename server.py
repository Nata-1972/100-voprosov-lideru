import json
import os
import smtplib
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
DATA_FILE = ROOT / "data" / "facilitator.json"
TOKEN = os.environ.get("FACILITATOR_BOT_TOKEN", "").strip()
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "3000"))
GAME_ACCESS_CODE = os.environ.get("GAME_ACCESS_CODE", "").strip()
LEADER_EMAIL = os.environ.get("LEADER_EMAIL", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME).strip()
LOCK = threading.Lock()
STOP_EVENT = threading.Event()
STATE = {"subscribers": [], "questions": [], "leaders": []}


def load_state():
    global STATE
    try:
        saved = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        STATE = {
            "subscribers": list(saved.get("subscribers", [])),
            "questions": list(saved.get("questions", []))[-100:],
            "leaders": list(saved.get("leaders", []))[-100:],
        }
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as error:
        print(f"Не удалось прочитать данные: {error}")


def save_state():
    with LOCK:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = DATA_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DATA_FILE)


def telegram(method, payload=None, timeout=35):
    encoded = urllib.parse.urlencode(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data=encoded
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Ошибка Telegram API"))
    return result.get("result")


def send_message(chat_id, text):
    telegram("sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)


def email_configured():
    return all((LEADER_EMAIL, SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM))


def send_email(subject, text):
    if not email_configured():
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = LEADER_EMAIL
    message.set_content(text)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)
    return True


def recent_questions():
    with LOCK:
        questions = list(STATE["questions"][-10:])
    if not questions:
        return "Пока ни одного вопроса не поступило."
    lines = []
    for number, item in enumerate(reversed(questions), 1):
        theme = f" · {item['interest']}" if item.get("interest") else ""
        lines.append(f"{number}. {item['question']}{theme}")
    return "\n\n".join(lines)


def recent_leaders():
    with LOCK:
        leaders = list(STATE["leaders"][-10:])
    if not leaders:
        return "Пока ни одного лидера не предложено."
    lines = []
    for number, item in enumerate(reversed(leaders), 1):
        lines.append(f"{number}. {item['leader']} · {item['sphere']}\n{item['reason']}")
    return "\n\n".join(lines)


def handle_bot_message(message):
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").split("@", 1)[0].strip()
    if not chat_id:
        return

    if text == "/start":
        with LOCK:
            occupied = bool(STATE["subscribers"] and chat_id not in STATE["subscribers"])
            if not occupied and chat_id not in STATE["subscribers"]:
                STATE["subscribers"].append(chat_id)
        if occupied:
            send_message(chat_id, "К этому боту уже подключён ведущий.")
            return
        save_state()
        send_message(
            chat_id,
            "Вы подключены как ведущий «100 вопросов лидеру». Новые вопросы "
            "будут приходить сюда без имён и контактов.\n\n"
            "/questions — последние 10 вопросов\n"
            "/leaders — последние 10 предложенных лидеров\n"
            "/status — состояние подключения\n"
            "/stop — отключить уведомления",
        )
    elif text == "/questions":
        with LOCK:
            active = chat_id in STATE["subscribers"]
        send_message(chat_id, recent_questions() if active else "Сначала отправьте /start.")
    elif text == "/leaders":
        with LOCK:
            active = chat_id in STATE["subscribers"]
        send_message(chat_id, recent_leaders() if active else "Сначала отправьте /start.")
    elif text == "/status":
        with LOCK:
            active = chat_id in STATE["subscribers"]
            total = len(STATE["questions"])
        send_message(
            chat_id,
            f"Подключение активно. Получено вопросов: {total}."
            if active
            else "Уведомления отключены. Отправьте /start, чтобы подключиться.",
        )
    elif text == "/stop":
        with LOCK:
            STATE["subscribers"] = [item for item in STATE["subscribers"] if item != chat_id]
        save_state()
        send_message(chat_id, "Уведомления отключены. Для повторного подключения отправьте /start.")
    else:
        send_message(chat_id, "Используйте /start, /questions, /leaders, /status или /stop.")


def polling_loop():
    offset = 0
    try:
        telegram(
            "setMyCommands",
            {
                "commands": json.dumps(
                    [
                        {"command": "start", "description": "Подключить ведущего"},
                        {"command": "questions", "description": "Последние 10 вопросов"},
                        {"command": "leaders", "description": "Предложенные лидеры"},
                        {"command": "status", "description": "Проверить подключение"},
                        {"command": "stop", "description": "Отключить уведомления"},
                    ],
                    ensure_ascii=False,
                )
            },
        )
        bot = telegram("getMe", timeout=15)
        print(f"Бот ведущего @{bot['username']} подключён.")
    except Exception as error:
        print(f"Не удалось подключить Telegram-бота: {error}")
        STOP_EVENT.set()
        return

    while not STOP_EVENT.is_set():
        try:
            updates = telegram("getUpdates", {"offset": offset, "timeout": 25}, timeout=35)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    handle_bot_message(message)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2)
        except Exception as error:
            print(f"Ошибка бота: {error}")
            time.sleep(2)


def deliver_question(item):
    with LOCK:
        subscribers = list(STATE["subscribers"])
    delivered = 0
    message = (
        "Новый вопрос ученика\n\n"
        f"{item['question']}\n\n"
        f"Тема: {item.get('interest') or 'не указана'}\n"
        f"Время: {item['receivedAt']}"
    )
    for chat_id in subscribers:
        try:
            send_message(chat_id, message)
            delivered += 1
        except Exception as error:
            print(f"Не удалось отправить сообщение ведущему: {error}")
    try:
        if send_email(
            "Новый вопрос ученика — 100 вопросов лидеру",
            "Новый анонимный вопрос ученика\n\n"
            f"{item['question']}\n\n"
            f"Тема: {item.get('interest') or 'не указана'}\n"
            f"Время: {item['receivedAt']}",
        ):
            delivered += 1
    except Exception as error:
        print(f"Не удалось отправить вопрос на email: {error}")
    return delivered


def deliver_leader(item):
    with LOCK:
        subscribers = list(STATE["subscribers"])
    question = item.get("question") or "не указан"
    text = (
        "Ученики предложили лидера\n\n"
        f"Лидер: {item['leader']}\n"
        f"Сфера: {item['sphere']}\n"
        f"Почему стоит пригласить: {item['reason']}\n"
        f"Вопрос лидеру: {question}\n"
        f"Время: {item['receivedAt']}"
    )
    delivered = 0
    for chat_id in subscribers:
        try:
            send_message(chat_id, text)
            delivered += 1
        except Exception as error:
            print(f"Не удалось отправить предложение в Telegram: {error}")
    try:
        if send_email("Ученики предложили лидера для встречи", text):
            delivered += 1
    except Exception as error:
        print(f"Не удалось отправить предложение на email: {error}")
    return delivered


class GameHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        # Не записываем IP-адреса участников в журнал.
        return

    def send_json(self, status, payload):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Game-Code")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Game-Code")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/status":
            with LOCK:
                connected = bool(STATE["subscribers"]) or email_configured()
            self.send_json(200, {"connected": connected})
            return
        if path in ("/", "/index.html"):
            content = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_json(404, {"error": "Не найдено."})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/questions", "/api/leaders"):
            self.send_json(404, {"error": "Не найдено."})
            return
        if GAME_ACCESS_CODE and self.headers.get("X-Game-Code", "") != GAME_ACCESS_CODE:
            self.send_json(403, {"error": "Неверный код игры."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                self.send_json(413, {"error": "Слишком большой запрос."})
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Некорректный запрос."})
            return

        received_at = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")
        if path == "/api/questions":
            question = body.get("question", "")
            interest = body.get("interest", "")
            question = question.strip() if isinstance(question, str) else ""
            interest = interest.strip()[:80] if isinstance(interest, str) else ""
            if not 12 <= len(question) <= 240:
                self.send_json(400, {"error": "Вопрос должен содержать от 12 до 240 символов."})
                return
            item = {"question": question, "interest": interest, "receivedAt": received_at}
            delivered = deliver_question(item)
            collection = "questions"
        else:
            leader = body.get("leader", "")
            sphere = body.get("sphere", "")
            reason = body.get("reason", "")
            question = body.get("question", "")
            leader = leader.strip() if isinstance(leader, str) else ""
            sphere = sphere.strip() if isinstance(sphere, str) else ""
            reason = reason.strip() if isinstance(reason, str) else ""
            question = question.strip() if isinstance(question, str) else ""
            if not 2 <= len(leader) <= 100 or not 2 <= len(sphere) <= 80:
                self.send_json(400, {"error": "Укажите лидера и сферу его деятельности."})
                return
            if not 8 <= len(reason) <= 500 or len(question) > 240:
                self.send_json(400, {"error": "Расскажите, почему этого лидера стоит пригласить."})
                return
            item = {
                "leader": leader,
                "sphere": sphere,
                "reason": reason,
                "question": question,
                "receivedAt": received_at,
            }
            delivered = deliver_leader(item)
            collection = "leaders"
        if not delivered:
            self.send_json(503, {"error": "Канал ведущего не настроен. Попробуйте позже."})
            return
        with LOCK:
            STATE[collection].append(item)
            STATE[collection] = STATE[collection][-100:]
        save_state()
        self.send_json(201, {"delivered": True})


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("8.8.8.8", 80))
            return connection.getsockname()[0]
    except OSError:
        return "адрес-компьютера"


def main():
    if not TOKEN and not email_configured():
        raise SystemExit("Настройте email или FACILITATOR_BOT_TOKEN для получения обращений.")
    load_state()
    if TOKEN:
        bot_thread = threading.Thread(target=polling_loop, daemon=True)
        bot_thread.start()
    if email_configured():
        print(f"Email ведущего настроен: {LEADER_EMAIL}")
    server = ThreadingHTTPServer((HOST, PORT), GameHandler)
    print(f"Игра на этом компьютере: http://localhost:{PORT}")
    print(f"Игра в локальной сети: http://{local_ip()}:{PORT}")
    print("Для остановки закройте окно или нажмите Ctrl+C.")
    threading.Timer(1, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        server.server_close()
        save_state()


if __name__ == "__main__":
    main()
