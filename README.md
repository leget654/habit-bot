# 🔥 Habit Tracker Bot

Telegram-бот для отслеживания привычек с inline-кнопками, стриками и статистикой.

## Возможности

- ✅ Отмечать привычки кнопками каждый день
- 🔥 Стрики — счётчик дней подряд
- 📊 Статистика: прогресс-бар, процент выполнения за месяц
- 📅 Календарь-сетка как на GitHub (✅ = выполнено)
- ⏰ Напоминания в выбранное время
- ➕ Добавлять / удалять привычки с эмодзи

---

## Быстрый старт

### 1. Получить токен бота

Открой [@BotFather](https://t.me/BotFather) в Telegram:
```
/newbot
```
Скопируй токен вида `7123456789:AAF...`

### 2. Установить зависимости

```bash
pip install -r requirements.txt
```

### 3. Запустить

```bash
BOT_TOKEN=7123456789:AAF... python bot.py
```

Или прописать токен прямо в `bot.py` — строка:
```python
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
```

---

## Команды бота

| Команда | Действие |
|---------|----------|
| `/start` | Начать, показать меню |
| `/menu` | Главное меню |

Всё остальное — через inline-кнопки.

---

## Структура файлов

```
habit_bot/
├── bot.py          # весь код бота
├── requirements.txt
├── README.md
└── habits.db       # создаётся автоматически при первом запуске
```

---

## Деплой (чтобы бот работал 24/7)

### Вариант A — VPS / сервер
```bash
# Установить как systemd-сервис
sudo nano /etc/systemd/system/habitbot.service
```
```ini
[Unit]
Description=Habit Tracker Bot

[Service]
ExecStart=/usr/bin/python3 /path/to/bot.py
Environment=BOT_TOKEN=ВАШ_ТОКЕН
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable habitbot
sudo systemctl start habitbot
```

### Вариант B — Railway.app (бесплатно)
1. Залить код на GitHub
2. Подключить репозиторий на [railway.app](https://railway.app)
3. Добавить переменную окружения `BOT_TOKEN`
4. Deploy ✅

### Вариант C — локально через `screen`
```bash
screen -S habitbot
BOT_TOKEN=... python bot.py
# Ctrl+A, D — выйти из screen не останавливая бота
```
