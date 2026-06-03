# Project Hub & AI Reporter

Self-hosted панель для агрегации проектных данных и автоматической генерации еженедельных статус-отчётов для учредителей.

## Возможности

- Внутренние проекты с несколькими источниками: YouTrack, IMAP-почта, CalDAV/iCal, Google Docs (Roadmap)
- Генерация отчёта одной кнопкой через DeepSeek или Claude
- Markdown-preview с редактированием, копированием и архивом отчётов
- Шифрование credentials в БД (Fernet)

## Развёртывание на другом компьютере (Git + Docker)

Инструкция для **приватного** репозитория: склонировать проект и поднять всё через Docker.
API-ключи можно хранить в `.env.example` в Git — на новой машине достаточно `cp .env.example .env`
(файл `.env` в репозиторий не попадает, но создаётся локально из шаблона).

### Что нужно на машине

- [Docker](https://docs.docker.com/get-docker/) и Docker Compose v2 (`docker compose version`)
- Git
- Доступ к приватному репозиторию (SSH или HTTPS)

Порты: **8000** (веб), внутри compose — **Redis** (наружу не пробрасывается).

### Шаги

```bash
# 1. Клонировать (подставьте URL вашего приватного репозитория)
git clone <URL-репозитория>
cd bankirosstat

# 2. Создать .env из шаблона (ключи уже в .env.example — при необходимости поправьте)
cp .env.example .env

# 3. Обязательно задать ENCRYPTION_KEY, если в .env он пустой:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Вставьте значение в .env → ENCRYPTION_KEY=...

# 4. Собрать и запустить в фоне
docker compose up --build -d

# 5. Проверить, что все три сервиса работают
docker compose ps
```

Откройте в браузере: **http://127.0.0.1:8000** (на другой машине в сети — `http://<IP-хоста>:8000`).

Поднимаются три контейнера:

| Сервис | Назначение |
|--------|------------|
| `redis` | Очередь фоновых задач |
| `app` | Веб (FastAPI), порт 8000 |
| `worker` | ARQ-воркер: отчёты, roadmap, синки, Gamma |

Без **worker** страница откроется, но генерация отчётов и импорт roadmap зависнут в очереди.

### После первого запуска

1. Зайти в **Проекты** — создать проект, привязать источники (YouTrack, почта, календарь, Google Doc/Sheet).
2. **Сотрудники** — добавить почты для сбора календаря/IMAP.
3. **Дашборд** — сгенерировать отчёт (задача уйдёт в worker, статус — виджет внизу справа).

Данные (SQLite, проекты, отчёты) хранятся в Docker-volume `app-data` и **не пропадают** при `docker compose down` (без `-v`).

### Обновление с Git на другом компе

```bash
cd bankirosstat
git pull
docker compose up --build -d
docker compose restart worker   # если менялись задачи воркера
```

### Полезные команды

```bash
docker compose logs -f app worker   # логи веба и воркера
docker compose restart app worker  # после смены .env (настройки кэшируются в app)
docker compose down                # остановить, данные сохраняются
docker compose down -v             # удалить volumes (БД и Redis — с нуля)
```

### Частые проблемы

| Симптом | Что проверить |
|---------|----------------|
| «Очередь задач недоступна» | `docker compose ps` — контейнер `redis` и `worker` в статусе Up |
| Отчёт не генерируется | `docker compose logs worker` — ошибки LLM/API; в `.env` есть ключ модели |
| Источники не сохраняются | В `.env` задан `ENCRYPTION_KEY` (не пустой) |
| Порт 8000 занят | В `docker-compose.yml` сменить `"8000:8000"` на `"8080:8000"` |
| Сменили `.env`, поведение старое | `docker compose restart app worker` |

**Redis в Docker:** в `docker-compose.yml` для `app` и `worker` уже прописано `REDIS_URL=redis://redis:6379/0`. Значение `REDIS_URL` в `.env` для локального запуска без compose; в Docker compose его переопределяет.

База SQLite в volume `app-data`. Чтобы видеть файл на диске хоста, в `docker-compose.yml` замените `- app-data:/app/data` на `- ./data:/app/data` (на Linux: `mkdir -p data && sudo chown 1000:1000 data`).

## Быстрый старт (если репозиторий уже склонирован)

Те же шаги, что выше, без `git clone`: `cp .env.example .env` → `docker compose up --build -d` → <http://127.0.0.1:8000>.

## Локальный запуск без Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполните ENCRYPTION_KEY и API-ключи LLM

# 1) Redis (нужен для фоновой очереди)
docker run -d --name project-hub-redis -p 6379:6379 redis:7-alpine

# 2) Веб-приложение
uvicorn main:app --reload

# 3) Воркер фоновых задач (отдельный терминал)
arq app.services.jobs.worker.WorkerSettings
```

## Фоновая очередь задач

Тяжёлые операции (генерация отчёта, импорт roadmap, извлечение задач из расшифровки)
выполняются в отдельном процессе-воркере через **ARQ + Redis** — веб-приложение
сразу отвечает `job_id`, а статус и результат хранятся в таблице `background_jobs`.

В правом нижнем углу любой страницы показан виджет активных задач — можно спокойно
переключаться между страницами, состояние не теряется.

Эндпоинты:

- `POST /api/reports/generate` → `{job_id}` — отчёт по проекту/портфелю
- `POST /api/projects/{id}/roadmap/save` → `{job_id}` — импорт roadmap из Google
- `POST /api/reports/{id}/sync` → `{job_id}` — извлечение задач из расшифровки
- `POST /api/sync-meetings/{id}/extract` → `{job_id}` — перегенерация задач
- `GET  /api/jobs/{id}` — статус и результат любой задачи
- `GET  /api/jobs/active` — список активных/только что завершённых задач

Запуск воркера: `arq app.services.jobs.worker.WorkerSettings`.

| Переменная | Описание |
|------------|----------|
| `REDIS_URL` | Брокер задач (по умолчанию `redis://localhost:6379/0`) |
| `JOBS_WORKER_TIMEOUT` | Таймаут одной задачи, секунды (по умолчанию `1800`) |
| `JOBS_WORKER_CONCURRENCY` | Сколько задач одновременно в одном воркере (по умолчанию `4`) |

## Настройка `.env`

Файл `.env` не коммитится (см. `.gitignore`). Шаблон `.env.example` — в Git; на новой машине: `cp .env.example .env`.
В приватном репозитории в `.env.example` могут лежать рабочие ключи команды.

| Переменная | Описание |
|------------|----------|
| `DATABASE_URL` | SQLite (по умолчанию) или PostgreSQL |
| `ENCRYPTION_KEY` | **Обязательно** — Fernet-ключ для шифрования credentials в БД |
| `ANTHROPIC_API_KEY` | Claude |
| `DEFAULT_LLM_MODEL` | Опционально: модель по умолчанию в UI (например `deepseek-v3`) |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `GAMMA_API_KEY` | Gamma API — презентации из отчётов |
| `GAMMA_TEMPLATE_ID` | File ID шаблона Gamma (`POST /generations/from-template`) |
| `GAMMA_LANGUAGE` | Язык слайдов (`ru`) |
| `REDIS_URL` | Для запуска **без** Docker; в compose подставляется автоматически |
| `JOBS_WORKER_TIMEOUT` | Таймаут задачи воркера, сек |
| `JOBS_WORKER_CONCURRENCY` | Параллельных задач в одном worker |

Сгенерировать `ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Минимум для работы отчётов: `ENCRYPTION_KEY` + хотя бы один из `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`. Для кнопки «Преза (Gamma)» в истории — `GAMMA_API_KEY` и `GAMMA_TEMPLATE_ID`.

## Страницы

1. **Дашборд** — выбор проекта и модели, генерация отчёта
2. **Проекты** — создание проектов, привязка источников и закрепление сотрудников
3. **Сотрудники** — ФИО и рабочая почта Yandex (IMAP `imap.yandex.ru`)
4. **История** — архив сохранённых отчётов

## Источники данных

- **YouTrack:** URL, token, project_id, board_name (несколько досок на проект)
- **Почта:** IMAP host, port, email, app password
- **Календарь:** CalDAV URL или публичная iCal-ссылка
- **Google Doc:** document_id (документ с доступом «по ссылке»)
- **Google Таблица:** spreadsheet_id, gid вкладки — Roadmap, KPI, эпики в табличном виде (CSV, доступ «по ссылке»)

Данные собираются за последние 7 дней.

## PostgreSQL

```env
DATABASE_URL=postgresql://user:pass@localhost:5432/project_hub
```
