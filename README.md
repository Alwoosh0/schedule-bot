# Schedule Bot

Телеграм-бот для еженедельного расписания: добавляешь занятие один раз (с днями недели), бот сам показывает его каждую неделю и присылает напоминания.

## Команды

- `/add ДНИ ЧЧ:ММ-ЧЧ:ММ Название` — добавить занятие
  - Дни: `mon,tue,wed,thu,fri,sat,sun` или по-русски `пн,вт,ср,чт,пт,сб,вс`, а также `weekdays` / `weekend` / `daily`
  - Примеры:
    - `/add mon,tue,wed,thu,fri 10:00-12:00 Java Backend`
    - `/add сб 08:30-09:30 Training`
    - `/add weekdays 12:30-13:30 IELTS Practice or DZ`
- `/today` — расписание на сегодня
- `/week` — расписание на всю неделю
- `/list` — все занятия с их ID
- `/del ID` — удалить занятие (ID смотри в `/list`)
- `/remind ID МИНУТЫ` — за сколько минут до начала присылать напоминание (по умолчанию 10, максимум 1440)
- `/help` — справка

## Структура

```
schedule_bot/
├── bot.py            # весь код бота
├── requirements.txt  # зависимости (python-telegram-bot + tzdata)
├── railway.json      # команда запуска и политика рестартов для Railway
├── Procfile          # то же для платформ, которые читают Procfile
├── .python-version   # версия Python (3.12)
├── .gitignore
└── README.md
```

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Что это |
|---|---|---|---|
| `BOT_TOKEN` | да | — | токен от @BotFather |
| `TIMEZONE` | нет | `Asia/Almaty` | часовой пояс расписания |
| `DB_PATH` | нет | `<RAILWAY_VOLUME_MOUNT_PATH>/schedule.db`, локально `./schedule.db` | путь к SQLite-базе |
| `DEFAULT_REMINDER_MIN` | нет | `10` | напоминание по умолчанию, мин |

Если к сервису на Railway подключён Volume, база автоматически кладётся в него — `DB_PATH` задавать не нужно.

## 1. Создать бота в Telegram

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → имя → username (должен заканчиваться на `bot`).
2. Получишь токен вида `123456789:AAH...`. Это пароль от бота — никуда не коммить. Если утёк: @BotFather → `/revoke`.

## 2. Локальный запуск (проверка)

```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
export BOT_TOKEN=твой_токен         # Windows (PowerShell): $env:BOT_TOKEN="твой_токен"
python bot.py
```

Остановить — `Ctrl+C`. **Перед запуском на Railway локальный бот должен быть остановлен**, иначе будет ошибка `Conflict: terminated by other getUpdates request`.

## 3. Деплой на Railway

1. Залей папку в приватный репозиторий GitHub.
2. [railway.com](https://railway.com) → войти через GitHub → New Project → Deploy from GitHub repo → выбрать репозиторий.
3. Первый деплой упадёт с `BOT_TOKEN не задан` — это нормально.
4. Сервис → Variables → добавить `BOT_TOKEN`.
5. Volume: правый клик по пустому месту на канвасе проекта (или `Ctrl/⌘+K` → Volume) → выбрать сервис бота → Mount path `/data`.
6. Нажать Deploy / Apply changes.
7. Domain генерировать не нужно — это worker, а не сайт.
8. В логах деплоя должно быть:
   ```
   Bot starting (timezone=Asia/Almaty, local time 2026-09-23 18:40, db=/data/schedule.db)...
   Scheduled reminders for 0 events
   ```
   Проверь, что `local time` совпадает с твоими часами, а `db` начинается с `/data/`.

Обновление: `git push` → Railway сам пересоберёт и перезапустит бота. Данные в Volume сохраняются.

## Частые проблемы

- `Conflict: terminated by other getUpdates request` — бот запущен в двух местах (скорее всего, локально и на Railway). Останови локальный.
- `BOT_TOKEN не задан` — не добавлена переменная или не нажат Deploy после добавления.
- `Volume не подключён — база ... будет стираться` в логах — не подключён Volume (шаг 5).
- Бот молчит — смотри Deploy Logs на Railway, статус сервиса должен быть Active.

## Дальнейшие идеи

- `/pdf` — экспорт расписания в PDF.
- `/import` — массовое добавление расписания списком.
- Ежедневная утренняя сводка ("вот твой день").
