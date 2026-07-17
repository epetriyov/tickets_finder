# tickets-watcher

Telegram-бот мониторинга билетов на события **Яндекс Афиши**. Каждые
`CHECK_INTERVAL_SECONDS` опрашивает недокументированный JSON API виджета
Яндекс Билетов и, как только находит **N соседних свободных мест в одном
ряду** не дороже заданной цены, присылает уведомление, где **каждая строка —
прямая ссылка, открывающая виджет с этими местами уже в корзине**: остаётся
нажать «Далее» и оплатить.

Проверен в бою: билеты на Басту в Лужниках (29.08.2026) куплены именно через
уведомление этого бота — возвраты в хорошие сектора живут минуты, и связка
«проверка раз в 30 сек + ссылка сразу в корзину» решает.

## Что умеет

- следит за конкретным сеансом события Афиши (дата выбирается из нескольких);
- фильтры: максимальная цена за билет (включительно), количество соседних
  мест, конкретные сектора (`C132-C139,A106`), исключение секторов
  «ограниченная видимость»;
- цепочки соседних мест схлопывает («места 8–14, 7 шт.» — одна строка),
  сортирует от дорогих к дешёвым, в ссылку кладёт самое дорогое окно;
- о каждом варианте сообщает один раз (`state.json`), повторов не шлёт;
- мониторинг самого себя: стартовое сообщение, heartbeat раз в 12 ч, алерт
  после 5 ошибок подряд, ✅ при восстановлении, Docker healthcheck;
- деплой на VPS одним `git push` (GitHub Actions → SSH → docker compose).

## Быстрый старт (5 минут, локально)

```bash
git clone https://github.com/epetriyov/tickets_finder && cd tickets_finder
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env    # шаги 1-3 ниже
```

1. **Токен бота**: создай бота у [@BotFather](https://t.me/BotFather)
   (`/newbot`) → `TELEGRAM_BOT_TOKEN`. Напиши своему боту `/start`, иначе он
   не сможет писать тебе первым.
2. **Свой chat_id**: напиши боту [@userinfobot](https://t.me/userinfobot),
   пришедшее число → `TELEGRAM_CHAT_ID`. (Классический способ через
   `getUpdates` не сработает, если у бота настроен webhook.)
3. **Событие**: ссылка на страницу события → `TARGET_URL`, дата сеанса →
   `SESSION_DATE`. Ключ сеанса и название бот определит сам.

Проверка и запуск:

```bash
venv/bin/python tickets_watcher.py --check   # что доступно прямо сейчас (без Telegram)
venv/bin/python tickets_watcher.py --test    # тестовое сообщение в Telegram
venv/bin/python tickets_watcher.py --force   # пример боевого уведомления со ссылкой
venv/bin/python tickets_watcher.py           # мониторинг (Ctrl+C для остановки)
```

Учти: при первом запуске бот пришлёт всё подходящее, что УЖЕ есть в продаже.

## Все настройки (.env)

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | токен бота от @BotFather (обязательно) |
| `TELEGRAM_CHAT_ID` | — | кому слать уведомления (обязательно) |
| `TARGET_URL` | страница Басты | ссылка на страницу события afisha.yandex.ru |
| `SESSION_DATE` | — | дата сеанса по МСК, `YYYY-MM-DD`; обязательна при нескольких датах |
| `SESSION_KEY` | пусто = авто | ключ сеанса виджета; вручную — только если автоопределение сломалось |
| `EVENT_NAME` | пусто = авто | название события в сообщениях |
| `CLIENT_KEY` | ключ виджета | clientKey официального виджета (менять при ошибках API, см. ниже) |
| `CHECK_INTERVAL_SECONDS` | `30` | период опроса API (реальный — с джиттером ±15%) |
| `MAX_PRICE_PER_TICKET` | `15000` | максимум за один билет, ₽, **включительно**, без сервисного сбора |
| `SEATS_NEEDED` | `2` | сколько соседних мест в одном ряду искать |
| `SECTORS` | пусто = все | сектора: `C132-C139,A106,A109` (диапазоны/одиночные, кириллица ок) |
| `IGNORE_LIMITED_VIEW` | `0` | `1` = молчать про сектора «(ограниченная видимость)» |
| `HEARTBEAT_HOURS` | `12` | период «я жив»-сообщений (можно дробное) |
| `ERROR_ALERT_THRESHOLD` | `5` | алерт после стольких ошибок API подряд |

⚠️ В Docker после правки `.env` нужен `docker compose up -d --force-recreate
watcher` (не `restart`: env вшивается при создании контейнера).

## Как это работает внутри

Схема зала на странице события — iframe `widget.afisha.yandex.ru`, у которого
есть открытый JSON API, отвечающий обычному GET без cookies и браузера:

```
GET https://widget.afisha.yandex.ru/api/tickets/v1/sessions/{SESSION_KEY}/hallplan/async?clientKey={CLIENT_KEY}
```

В ответе — только СВОБОДНЫЕ места: сектор, ряд, место, цена и сбор в копейках
(подробный разбор формата — в шапке [afisha_api.py](afisha_api.py)).
Дополнительно опрашивается `/seat-locks` (места в чужих корзинах).

`SESSION_KEY` — base64 от `…|eventId|…|timestampMs`; такие ключи вшиты в HTML
страницы события, оттуда бот и берёт их автоматически (`resolve_session`).

Ссылка «в корзину» использует недокументированный параметр виджета
`?selectedSeats=<url-encoded JSON [{"level":<id>,"row":"..","place":".."}]>` —
виджет сам кладёт места в корзину, если все они ещё свободны (иначе просто
откроется схема). Найден в JS-бандле виджета, проверен вживую. В мобильное
приложение Афиши такую ссылку передать нельзя: домен виджета не зарегистрирован
для universal/app links, а `selectedSeats` понимает только веб-виджет — ссылки
открываются в браузере, где всё работает.

## Деплой на VPS (Docker + GitHub Actions)

Деплой на **каждый пуш в main**: `.github/workflows/deploy.yml` заходит на
VPS по SSH, обновляет код до `origin/main`, пересобирает docker compose.

Одноразовая подготовка VPS (нужен Docker):

```bash
git clone https://github.com/epetriyov/tickets_finder ~/tickets_finder
cd ~/tickets_finder
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f watcher
```

Одноразово в GitHub (Settings → Secrets and variables → Actions):
`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` (приватный ключ без пассфразы —
лучше отдельный деплой-ключ), `VPS_APP_DIR` (путь к клону на VPS).

Дальше каждый `git push origin main` деплоится сам. `state.json`,
`watcher.log` и `health.json` живут в `./data` (volume) и переживают
пересборку — повторных уведомлений после деплоя не будет.

<details>
<summary>Альтернатива без Docker: systemd</summary>

```bash
# поправь пути и пользователя внутри tickets-watcher.service, затем:
sudo cp tickets-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tickets-watcher
journalctl -u tickets-watcher -f
```
</details>

## Мониторинг: как понять, что бот жив

- **стартовое сообщение** при каждом запуске/деплое (заодно видно текущие критерии);
- **heartbeat** раз в `HEARTBEAT_HOURS`: «💓 работает: проверок N, ошибок M,
  вариантов сейчас K» — перестал приходить, значит процесс мёртв;
- **алерты**: ⚠️ после `ERROR_ALERT_THRESHOLD` ошибок API подряд (повтор не
  чаще раза в час) и ✅ при восстановлении;
- **Docker healthcheck**: `docker compose ps` покажет `unhealthy`, если цикл
  завис (бот обновляет `data/health.json` после каждой итерации — там же
  время последней проверки и последняя ошибка).

## Команды бота

`/check` — что доступно сейчас по настроенным критериям; `/check all` — по
всему залу без фильтров; `/start`, `/help` — справка. Отвечает только чату
из `TELEGRAM_CHAT_ID`.

⚠️ Если у бота настроен webhook (бот используется другим сервисом), Telegram
не даёт делать long polling — команды отключатся сами (warning в логе),
уведомления при этом работают. Решение: отдельный бот для вотчера.

## Если что-то сломалось

| Симптом | Что делать |
|---|---|
| `--check` падает «ответ не JSON (капча/антибот?)» | Яндекс ограничил IP: увеличь `CHECK_INTERVAL_SECONDS` (120–300) или смени IP |
| «не удалось определить сеанс» | проверь, что `TARGET_URL` ведёт на страницу события; задай `SESSION_DATE`; в крайнем случае впиши `SESSION_KEY` вручную (DevTools → Network → фильтр `hallplan` → ключ в URL) |
| API отвечает ошибкой при верном ключе | у Яндекса сменился `CLIENT_KEY`: открой любой виджет, DevTools → Network → `hallplan` → параметр `clientKey` → в `.env` |
| Формат ответа изменился | правь `extract_seats()` в `afisha_api.py`, структура задокументирована в шапке файла; контракт зафиксирован тестами |

## Структура проекта

```
tickets_watcher.py   # главный цикл: проверки, уведомления, heartbeat, команды бота, флаги CLI
afisha_api.py        # всё про API Афиши: hallplan, seat-locks, resolve_session, поиск цепочек, buy_link
telegram_notify.py   # sendMessage / getUpdates
tests/               # pytest: контракт парсера API и логика поиска (запускается в CI)
Dockerfile, docker-compose.yml, .github/workflows/{ci,deploy}.yml
tickets-watcher.service  # альтернатива systemd
```

## Дисклеймер

Личный инструмент. Использует недокументированные эндпоинты виджета Яндекс
Билетов — они могут измениться в любой момент. Бот ничего не покупает сам:
только следит и присылает ссылки, покупка — руками. Не гоняй с агрессивными
интервалами: боту хватает 30 секунд.
