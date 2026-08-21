# MyTube — SETUP

Короткая инструкция фазы 1 (D-37): переменные `.env`, порядок запуска, что
прокинуть во внешний реверс-прокси, как проверить кэш, бэкап и режимы кэша.
В фазе 6 этот файл вырастает в честный README по DIST-04 — здесь он
намеренно минимален.

Каждая команда ниже либо реально выполнена при написании этого файла на
живом стеке (`docker compose ps` — все сервисы healthy), либо помечена как
непроверенная явно.

## Что это и чего не умеет

Self-hosted раздача progressive MP4 для просмотра синхронной компанией
7–15 друзей в браузере и в плеере AVPro внутри VRChat.

Три честных ограничения:

- Каждый зритель в VRChat обязан **один раз** включить в настройках
  «Allow Untrusted URLs» — без этого AVPro не откроет ни один внешний
  адрес, включая свой собственный.
- **Публичные и group-public инстансы VRChat не поддерживаются** начиная
  с билда 1546 — только приватные и friends-инстансы.
- **Адаптивного битрейта нет и не будет.** Разные качества (если
  понадобятся) живут как разные издания одного тайтла, не как один поток
  с переключением на лету — ABR подтверждённо не работает в VRChat.

## Переменные `.env`

Скопировать `.env.example` в `.env` и заполнить. Значения из
`.env.example` — рабочие дефолты для локального стенда, кроме отмеченных
«обязательно сгенерировать».

| Переменная | Назначение | По умолчанию | Обязательность |
|---|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Учётные данные Postgres, потребляются образом `postgres:16-alpine` напрямую | `mytube` / `change-me...` / `mytube` | Пароль обязательно сгенерировать |
| `MYTUBE_SECURE_LINK_SECRET` | Секрет подписи `/blob/`-адресов (D-01/D-02) | — | Обязательно сгенерировать: `openssl rand -hex 32`. **В репозиторий не попадает** — `.env` в `.gitignore` |
| `MYTUBE_SIGN_TTL_SECONDS` | Срок жизни подписи, секунды (D-03) | `43200` (12ч) | Опционально |
| `MYTUBE_DATABASE_URL` | Строка подключения к Postgres, должна совпадать с `POSTGRES_*` выше | `postgresql://mytube:...@db:5432/mytube` | Обязательно держать в синхроне с `POSTGRES_*` |
| `MYTUBE_S3_ENDPOINT` | S3-эндпоинт хранилища. Если указан внешний — используется он (D-16) | `http://storage:8333` | Опционально |
| `MYTUBE_S3_UPSTREAM` | `host:port` того же хранилища для `upstream storage-internal` внутри edge | `storage:8333` | **Обязательно менять вместе с `MYTUBE_S3_ENDPOINT`** — см. предупреждение ниже (вердикт M7) |
| `MYTUBE_S3_BUCKET` / `MYTUBE_S3_REGION` | Имя бакета и произвольный непустой регион | `mytube` / `mytube` | Опционально |
| `MYTUBE_S3_KEY` / `MYTUBE_S3_SECRET` | Учётные данные S3-идентичности `mytube` (Admin/Read/Write/List/Tagging) | — | Обязательно сгенерировать: `openssl rand -hex 16` / `openssl rand -hex 32` |
| `MYTUBE_DELETE_GRACE_SECONDS` | Окно между `410` и физическим удалением объекта (D-13) | `300` (5 мин) | Опционально |
| `MYTUBE_CACHE_MODE` | `memory` или `off` — выбирает `edge/cache-${MYTUBE_CACHE_MODE}.conf.template` (DELIV-08) | `memory` | Опционально, см. «Режимы кэша» |
| `MYTUBE_CACHE_SIZE` | Размер зоны кэша в tmpfs (D-23) | `512m` | Опционально |
| `MYTUBE_CACHE_INACTIVE` | Окно вытеснения неактивных кусков (D-24) | `120s` | Опционально |
| `MYTUBE_SLICE_SIZE` | Размер куска для `nginx slice` | `1m` | Опционально |
| `MYTUBE_CACHE_LOCK_TIMEOUT` | `proxy_cache_lock_timeout` — держать заметно выше дефолта nginx (Pitfall 11) | `20s` | Опционально |
| `MYTUBE_EDGE_PORT` | Порт, на котором edge слушает на хосте | `8080` | Опционально |
| `MYTUBE_BACKUP_DIR` | Каталог дампов внутри контейнера `db-backup`, смонтирован на `./data/backups` хоста | `/backups` | Опционально |
| `MYTUBE_BACKUP_KEEP_DAYS` | Сколько дней хранить дампы перед удалением | `7` | Опционально |
| `MYTUBE_WARM_BYTES` | Предел прогрева кэша в байтах (вердикт M5) | `268435456` (половина зоны 512m) | Опционально |

**Про `MYTUBE_S3_UPSTREAM` отдельно (вердикт M7).** `api` ходит в
хранилище по `MYTUBE_S3_ENDPOINT`, `edge` — по `MYTUBE_S3_UPSTREAM`. Это
два разных адреса на одно и то же хранилище: если сменить один и забыть
второй, загрузка (через `api`) и выдача (через `edge`) уйдут в разные
места, и поломка вскроется не сразу, а на первом же просмотре нового
тайтла. При переходе на внешнее хранилище (D-16) **меняются обе
переменные одновременно**.

Отдельное ограничение фазы 1: внешнее хранилище обязано быть достижимо
**только из внутренней сети владельца** по обычному HTTP. D-38 требует,
чтобы хранилище было недостижимо снаружи — публичный S3-эндпоинт нарушал
бы это требование сам по себе, независимо от прочих настроек.

## Prerequisites

Before the first command below can run, the machine needs Docker Desktop
with Compose v2. Install it from the [official Docker Desktop for Windows
guide](https://docs.docker.com/desktop/setup/install/windows-install/)
rather than a copy of it here — a second copy of somebody else's install
guide rots, and this project would then be maintaining two.

Check the install from a shell before continuing:

```bash
docker version
docker compose version
```

This stack is known to run on Docker Engine 29.7.2 with Compose v5.4.0;
an older Docker Desktop that still ships Compose v1 (a separate
`docker-compose` binary, with a hyphen, no `version` output matching the
above) is not what any command in this document was written against.

If `docker version` reports the command not found even though Docker
Desktop is installed, its CLI is not on this shell's `PATH` — some Docker
Desktop installs place it under a per-user directory
(`%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin`) that is not always
added automatically. Add that directory to `PATH`, or open a fresh
terminal after installing.

Past Docker itself, the commands in this document run a handful of other
binaries directly on the host, not inside a container. On a machine with
only Docker Desktop and Git for Windows installed:

| Binary | Ships with |
|---|---|
| `docker`, `docker compose` | Docker Desktop |
| `bash`, `cp` | Git for Windows |
| `openssl`, `curl` | Git for Windows (its bundled MinGW toolchain) |
| `ffprobe` | **Neither.** Required only for the decodability check under "Verifying served video is actually decodable" below — install it separately (for example, an [ffmpeg Windows build](https://www.gyan.dev/ffmpeg/builds/)) and put it on `PATH` before running that one command. |

This document's own promise is that every command in it was either really
run or is explicitly marked as not. Installing Docker Desktop itself and
pulling this stack's four images for the first time have not been walked
by following this document on a machine that started with neither —
`docker compose up -d --wait` below has only ever run where the images
were already present. A registry lookup for each image reference
(`postgres:16-alpine`, `chrislusf/seaweedfs:3.97`, `nginx:1.27-alpine`,
`python:3.13-slim`) confirms each one still resolves to a real,
`linux/amd64`-capable manifest — that is evidence the pull should succeed,
not a record that it has.

## Порядок запуска

```bash
cp .env.example .env
# заполнить .env — как минимум MYTUBE_SECURE_LINK_SECRET, MYTUBE_S3_KEY,
# MYTUBE_S3_SECRET, POSTGRES_PASSWORD (см. таблицу выше)

docker compose up -d --wait
```

Порядок именно такой — **сначала поднять стек, а не «сначала накатить
миграции»** (вердикты M1, m2). База живёт в этом же compose, поэтому
накатить миграции до её подъёма физически невозможно, а отдельного
одноразового сервиса миграций в стеке намеренно нет: `docker compose up
--wait` считает выход контейнера отказом, и разовый init-сервис сломал
бы именно эту команду. Вместо этого `docker-entrypoint.sh` сервиса `api`
делает `alembic upgrade head`, затем заводит бакет хранилища
(`ensure_bucket()`) при **каждом** старте — обе операции идемпотентны,
рестарт безопасен.

Проверить, что всё поднялось:

```bash
docker compose ps            # все сервисы — healthy
docker compose logs api      # ищем строку про успешную миграцию
```

Если `ensure_bucket()` не смог завести бакет через S3 API, точная команда
штатного пути для выбранного хранилища (SeaweedFS) есть в
`docs/experiments/07-object-storage.md` — выполнить её и перезапустить
`api`.

Завести первый тайтл:

```bash
docker compose exec api python -m orchestrator.cli add \
  --file /ingest/<файл в src/ingest/> --name "<название>"
```

Команда напечатает адрес вида `/v/<слаг>`.

## Windows / WSL2 deployment notes

Found while closing out the phase-1 acceptance gate on a real Windows host.
None of this is Linux-specific advice — it only applies if the compose
stack runs on Windows with Docker Desktop's WSL2 backend. None of the
commands in this section have been walked by following this document
top-to-bottom on a machine that started with WSL2 unconfigured — what
follows is corroborated against artifacts already present on one
already-configured deployment, not a record of a fresh install-and-
configure run. That deployment's `.wslconfig` does set
`networkingMode=Mirrored`, the precondition this whole section assumes; if
yours does not set that, none of it applies to you.

**Published port unreachable from the LAN even though `docker compose ps`
looks fine and Windows Firewall is disabled.** If Docker Desktop's WSL2
integration uses `networkingMode: mirrored`, the edge port is reachable on
`127.0.0.1` but the actual gate for anything coming from the LAN or the
internet is the **Hyper-V firewall** governing the WSL VM, not Windows
Firewall. It defaults to `DefaultInboundAction: Block` and ships only ICMP
and mDNS allow rules — a published container port never gets an explicit
allow rule on its own. Symptom: the reverse proxy (or any machine other
than the host itself) cannot reach the edge at all, while everything works
from `127.0.0.1`. Fix:

```powershell
# Look up your own VM's creator ID first -- do not reuse this from another
# machine, it is not a universal constant.
Get-NetFirewallHyperVVMSetting

# Scoped default: only the given remote address or subnet may reach the
# edge port. Replace <reverse-proxy-address-or-subnet> with the reverse
# proxy host's address, or the LAN subnet if more than one machine needs
# it -- see the security note below before running this.
New-NetFirewallHyperVRule -Name "MyTube-Edge-8080" -DisplayName "MyTube edge (HTTP 8080)" `
  -Direction Inbound -VMCreatorId '<your VMCreatorId from the command above>' `
  -RemoteAddress '<reverse-proxy-address-or-subnet>' `
  -Protocol TCP -LocalPorts 8080 -Action Allow

# Unscoped form -- only if the edge is genuinely meant to be reachable from
# any address that can reach this machine at all (omitting -RemoteAddress
# defaults it to Any):
# New-NetFirewallHyperVRule -Name "MyTube-Edge-8080" -DisplayName "MyTube edge (HTTP 8080)" `
#   -Direction Inbound -VMCreatorId '<your VMCreatorId from the command above>' `
#   -Protocol TCP -LocalPorts 8080 -Action Allow
```

**What this rule opens.** Without `-RemoteAddress`, this admits inbound TCP
to the edge port from any remote address that can reach the VM at all —
the block-by-default posture the Hyper-V firewall normally provides is
gone for this one port, for anyone, not just the reverse proxy. Scope it
to the reverse proxy host or the LAN subnet, as the default form above
does; keep the unscoped form only for the case where the edge is
genuinely meant to be reachable from anywhere, which this project's own
design does not call for — external traffic is already meant to arrive
through the owner's own reverse proxy (see "Что прокинуть в
реверс-прокси" below), not directly at this port.

On one already-configured deployment, a Hyper-V rule matching this exact
command — same name, display name, and that VM's own creator ID — is
present, which corroborates that this command (or one equivalent to it)
was applied there; it does not establish that running it from a blocked
state was directly observed to restore reachability, since no
before/after test accompanies it. That same host also carries a second,
broader Hyper-V rule this document never instructed creating
(`VMCreatorId: Any`, unscoped by remote address), so observed reachability
on it cannot be attributed to the command above alone.

**Git Bash mangles container-absolute paths.** Running the `add` command
above (or any `docker compose exec ... --file /ingest/...`) from Git Bash
on Windows silently rewrites `/ingest/x.mp4` into something like
`C:/Program Files/Git/ingest/x.mp4` before it reaches the container --
Git Bash's POSIX-path auto-conversion does not know the path is meant for
the container, not the host. Prefix the command with `MSYS_NO_PATHCONV=1`:

```bash
MSYS_NO_PATHCONV=1 docker compose exec api python -m orchestrator.cli add \
  --file /ingest/<file> --name "<title>"
```

Scope `MSYS_NO_PATHCONV=1` to this one command, not the whole shell session
-- exporting it globally also disables Git Bash's path translation for
tools like `curl -o /d/some/local/path`, which then silently fail to write
their local output file while still reporting success.

**Viewer-side VPN trap, not a service defect.** At least one viewer's VPN
client ("throne") had to be switched from its default mode to TUN mode
before the address worked at all. If a friend reports the link does not
load, ask about their VPN client's mode before debugging the server.

## Что прокинуть в реверс-прокси

Наружу через внешний реверс-прокси владельца (D-19/D-20) выставляются
**ровно четыре** пути: `/v/`, `/p/`, `/blob/` и `/refresh/`. Всё
остальное — админка, консоль хранилища, `/healthz` — остаётся в LAN
(D-21). Отдельного пути доступа изнутри LAN в обход внешнего канала нет
намеренно: один адрес и один кодовый путь для всех, включая владельца
(D-22) — так владелец видит поломку первым, теми же глазами, что и
друзья.

**Обязательная проверка: внешний прокси не должен буферизовать ответ и
обязан пропускать `Range` целиком.** Это классическая грабля двух nginx
подряд — если внешний прокси собирает весь ответ перед отправкой клиенту,
`slice`-кэш работает вхолостую, а сам ответ на Range-запрос вернётся
целым файлом вместо `206`. Проверяется одной командой через внешний
домен:

```bash
curl -r 0-1048575 -D- -o /dev/null "https://<домен>/blob/<edition_id>.mp4?exp=...&sig=..."
```

Ожидается код `206` и заголовок `Content-Range` вида
`bytes 0-1048575/<полный размер>`. Код `200` с целым файлом означает, что
внешний прокси буферизует — чинить нужно на его стороне, не в compose.

## Verifying served video is actually decodable

A `200`/`206` with the right `Content-Length` is proof the delivery
mechanics work -- it is **not** proof the bytes are playable video. The
phase-1 acceptance record was once validated against 3 MiB of random bytes
that happened to have a plausible size; the delivery chain looked perfect
and nothing would have opened in a player (see
`docs/experiments/02-05-avpro-redirect-and-cache.md`). Use
`tools/check_decodable.sh <url-or-path>` after adding any new title to
assert the served bytes actually decode as video, not merely that a
request for them succeeded:

```bash
bash tools/check_decodable.sh "https://<домен>/blob/<edition_id>.mp4?exp=...&sig=..."
```

`ffprobe` on this machine reorders `-of csv=p=0` output, so the script
parses `-of json` by field name instead -- do not "simplify" it back to csv.

## Как проверить кэш

Прогреть кэш **непосредственно перед сеансом** (D-27):

```bash
docker compose exec api python -m orchestrator.cli cache-warm <слаг>
```

Записать текущее время (для `--since` ниже), провести сеанс, затем
посчитать:

```bash
docker compose exec api python -m orchestrator.cli cache-report --since <записанное время>
```

Критерий приёмки (D-28): **ноль кусков с более чем одним запросом к
хранилищу**, то есть код возврата `0`. Доля `HIT` печатается справочно —
приёмочным числом она не является: при N синхронных зрителях на холодном
кэше её теоретический потолок — `(N-1)/N`, то есть 67% на троих.

**Ненулевой код `cache-report` без списка кусков-нарушителей означает не
«всё плохо», а «мерить нечем»** (вердикт B2) — например, лог пуст или
`--since` отфильтровал всё. В этом случае первым делом проверить, что в
`edge/nginx.conf.template` для `location ~ ^/blob/` включена директива
`log_subrequest on;` — без неё nginx не логирует slice-подзапросы вообще,
и `cache-report` физически нечего разбирать.

**Честная арифметика прогрева (вердикт M5).** Зона кэша 512 МБ (D-23) при
профиле 1080p ~6 Мбит/с вмещает около 11 минут видео, а `inactive` ~2
минуты (D-24) выкидывает всё, к чему не обращались это время. Поэтому
`cache-warm` по умолчанию греет только начало файла — это единственное,
что прогрев реально лечит (стартовый стойл), — и по той же причине
обещание D-26 «перезапуск хранилища посреди вечера незаметен» действует
ровно в пределах окна `inactive` после того, как хранилище снова стало
доступно, а не бесконечно долго.

**При интерпретации лога вручную:** `cache-warm` шлёт свои собственные
точечные Range-запросы, и каждый из них `cache-report` привязывает к
куску, который он реально запросил, — попадание прогрева в окно измерения
стоит не больше одного запроса к хранилищу на кусок, а не ложное
нарушение. Причина по-прежнему мерить сеанс от напечатанной метки —
увидеть сам сеанс, а не растянутый на весь прогрев начальный отрезок
файла. Строки, записанные до обновления edge, которое добавило это поле
смещения, привязать нельзя — `cache-report` считает их отдельно, и если
всё окно состоит только из них, сообщает «недостаточно данных», а не
чистое прохождение. `cache-warm` печатает инструкцию с `--since` сам после
каждого прогона.

**Замечено при проверке на живом стеке:** `$time_iso8601` в логе имеет
разрешение в одну секунду. Если записать `--since` и тут же, в ту же
секунду, начать сеанс — хвост прогрева и начало сеанса могут попасть в
один и тот же таймстемп и дать точно такое же ложное нарушение. На
реальном сеансе (минуты между прогревом и тем, когда компания
действительно смотрит) это не встречается — упомянуто здесь только
потому, что при автоматической проверке этого файла воспроизвелось
у самого исполнителя плана.

## Бэкап и восстановление

**Почему это есть уже в фазе 1** (D-15): видеофайлы можно залить заново,
историю слагов — нет. Сервис `db-backup` в `docker-compose.yml` раз в
сутки снимает `pg_dump` в сжатый файл с датой в имени, кладёт в
`./data/backups` на хосте (то же дерево `/data/`, что уже в
`.gitignore`) и удаляет дампы старше `MYTUBE_BACKUP_KEEP_DAYS` дней.

Разовый прогон вручную (например, прямо перед рискованной операцией):

```bash
docker compose exec db-backup bash /backup/backup.sh --once
```

**Восстановление** (проверено на живом стеке в реальном случае — дамп
восстановлен поверх **непустой** базы, все слаги вернулись, ссылка
играет):

```bash
# 1. Остановить api. Это не формальность: шаг 3 роняет таблицы, и api,
#    держащий к ним соединения, на этом завершится.
docker compose stop api

# 2. Убедиться, что база поднята
docker compose up -d --wait db

# 3. Восстановить последний дамп.
#    ON_ERROR_STOP=1 обязателен: без него psql проглатывает все ошибки и
#    выходит с кодом 0, то есть полностью провалившееся восстановление
#    рапортует об успехе.
LATEST=$(docker compose exec -T db-backup sh -c 'ls -t /backups/mytube-*.sql.gz | head -1')
docker compose exec -T db-backup sh -c "gunzip -c $LATEST" \
  | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
echo "код возврата: $?"   # 0 — восстановлено; иначе НЕ восстановлено

# 4. Поднять остальное. api без этого шага не вернётся сам.
docker compose up -d --wait
```

Дамп снимается с `pg_dump --clean --if-exists`, поэтому он самодостаточен:
кладётся и на пустую базу, и поверх существующей. Это важно, потому что
восстанавливаются, когда данные **есть** и они неверны, а не когда их нет.

**Дампы, снятые до появления этого флага, поверх непустой базы не лягут** —
упадут на первом `CREATE TABLE ... already exists`. Снимите свежий дамп
(`--once` выше) и проверьте, что восстановление проходит, до того как он
понадобится всерьёз.

**Под Git Bash на Windows** команды с путём внутри контейнера требуют
префикса `MSYS_NO_PATHCONV=1`, иначе `/backup/backup.sh` превращается в
`C:/Program Files/Git/backup/backup.sh`.

## Режимы кэша

`MYTUBE_CACHE_MODE` выбирает между `edge/cache-memory.conf.template` и
`edge/cache-off.conf.template` (DELIV-08):

- **`memory`** — рабочий режим. `proxy_cache` в tmpfs (`/dev/shm`),
  ключ кэша `$uri$slice_range` (без `$arg_sig`/`$arg_exp` — иначе кэш
  делился бы по подписи каждого зрителя, и общий просмотр компанией не
  экономил бы ни одного запроса к хранилищу).
- **`off`** — кэш выключен полностью. Нужен для диагностики: если
  поведение (например, число запросов к хранилищу) отличается только в
  `memory`, проблема в самом кэше, а не в `slice`/подписи/хранилище.

Переключение — сменить `MYTUBE_CACHE_MODE` в `.env` и перезапустить
`edge`: `docker compose up -d --wait edge`.
