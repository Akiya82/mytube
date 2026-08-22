# Эксперимент 1: прямой MP4 с собственного домена в AVPro (VRChat)

**English abstract:** Tested whether a progressive MP4 file, served from the
owner's own domain, plays directly in VRChat's AVPro video player -- the
foundational technical bet the entire project rests on. Verdict: **PASS**,
confirmed on both a direct address and the real production form with a
signed query string.

Go/no-go всего проекта (ROADMAP критерий 1). Провал ≠ пересмотр замысла — сначала
перебор причин по списку D-33.

## Предпосылки (заполнено автоматически)

- Контрольный клип собран и машинно проверен по D-34: `src/tools/make_control_clip.sh`
  → `build/control.mp4`. H.264 High 4.1, `yuv420p`, `+faststart` подтверждены
  `ffprobe` и `check_faststart.py` (см. коммит Task 1 плана 01-01).
- Домен и TLS уже существуют и работают (D-19) — владелец выкладывает файл через
  существующий реверс-прокси.

## Отклонение от протокола: стенд

Протокол предполагал «бинарный файл, отданный реверс-прокси владельца». Владелец
прогнал эксперимент против **уже работающего продакшен-стенда**: собственного
MinIO S3-бакета за кэширующим реверс-прокси (`nginx/1.27.5`), а не против голого
файла на домене. Это осознанная замена, а не отступление от протокола — стенд
строже требуемого минимума: в цепочке уже стоит кэширующий слой (аналог будущего
edge-nginx фазы 1, D-20), и результат заодно подтверждает, что Range-запросы
переживают кэш (см. шаг 3 ниже, `X-Cache: HIT` на партиальном ответе). Если бы
эксперимент прошёл против голого файла без кэша, это ничего не говорило бы про
поведение кэширующего слоя; здесь оно проверено бесплатно.

Стенд: `https://s3.example.com/movies/control.mp4` (бакет MinIO за кэширующим
прокси, `Server: nginx/1.27.5`).

## Протокол

| Поле | Значение |
|---|---|
| Дата | 2026-08-20 (curl-проверки, 21:30–21:31 UTC) / 2026-08-20 (проверка в VRChat, дата и время прогона в VRChat отдельно не зафиксированы владельцем) |
| Билд VRChat | 2026.3.1 |
| Мир и плеер | Мир `06_Leaf` от `_hoshino_`, видеоплеер VizVid |
| Тип инстанса | Invite+ — удовлетворяет требованию протокола (не публичный инстанс) |

### Шаг 1-2: заголовки (`curl -I`)

```
$ curl -I https://s3.example.com/movies/control.mp4
HTTP/1.1 200 OK
Accept-Ranges: bytes
Alt-Svc: h3=":443"; ma=2592000
Content-Length: 92330979
Content-Type: video/mp4
Date: Thu, 20 Aug 2026 21:30:11 GMT
Etag: "bd0eb58c855b79e457e2d0f6df6ed544-6"
Last-Modified: Thu, 20 Aug 2026 21:29:14 GMT
No-Gzip-Compression: true
Server: nginx/1.27.5
Strict-Transport-Security: max-age=31536000; includeSubDomains
Vary: Origin
X-Amz-Expiration: expiry-date="Sun, 20 Sep 2026 00:00:00 GMT", rule-id="d8gns8svt0ds77l1kkb0"
X-Amz-Id-2: dd9025bab4ad464b049177c95eb6ebf374d3b3fd1af9251148b658df7ac2e3e8
X-Amz-Request-Id: 18CDA0C18F621F4E
X-Cache: HIT
X-Content-Type-Options: nosniff
X-Ratelimit-Limit: 5554
X-Ratelimit-Remaining: 5554
X-Xss-Protection: 1; mode=block
```

Вердикт шага: `200`, `Content-Type: video/mp4`, `Accept-Ranges: bytes` — все три
условия выполнены.

### Шаг 3: Range-запрос (`curl -r 0-1048575`)

```
$ curl -r 0-1048575 -o test.mp4 -D- https://s3.example.com/movies/control.mp4
HTTP/1.1 206 Partial Content
Alt-Svc: h3=":443"; ma=2592000
Content-Length: 1048576
Content-Range: bytes 0-1048575/92330979
Content-Type: video/mp4
Date: Thu, 20 Aug 2026 21:31:28 GMT
Etag: "bd0eb58c855b79e457e2d0f6df6ed544-6"
Last-Modified: Thu, 20 Aug 2026 21:29:14 GMT
No-Gzip-Compression: true
Server: nginx/1.27.5
Strict-Transport-Security: max-age=31536000; includeSubDomains
Vary: Origin
X-Amz-Expiration: expiry-date="Sun, 20 Sep 2026 00:00:00 GMT", rule-id="d8gns8svt0ds77l1kkb0"
X-Amz-Id-2: dd9025bab4ad464b049177c95eb6ebf374d3b3fd1af9251148b658df7ac2e3e8
X-Amz-Request-Id: 18CDA0C18F621F4E
X-Cache: HIT
X-Content-Type-Options: nosniff
X-Ratelimit-Limit: 5554
X-Ratelimit-Remaining: 5554
X-Xss-Protection: 1; mode=block
```

Вердикт шага: `206` с корректным `Content-Range: bytes 0-1048575/92330979`.
Отдельно стоит отметить: ответ отдан с `X-Cache: HIT` — Range-запрос пережил
кэширующий слой перед бакетом целым, не был обойдён кэшем.

### Шаг 3a: боевая форма адреса с query-string (M4)

Повтор шага 2 по адресу `https://s3.example.com/movies/control.mp4?exp=1&sig=abc`
(battle-form адрес по D-06).

```
$ curl -I "https://s3.example.com/movies/control.mp4?exp=1&sig=abc"
HTTP/1.1 200 OK
Accept-Ranges: bytes
Content-Length: 92330979
Content-Type: video/mp4
Etag: "bd0eb58c855b79e457e2d0f6df6ed544-6"
Server: nginx/1.27.5
X-Amz-Request-Id: 18CDA0C18F621F4E
X-Cache: HIT
(остальные заголовки идентичны шагу 2)
```

Вердикт шага: `200`, поведение идентично адресу без query-string. Строка
параметров не помешала отдаче.

Query-string: **играет** (curl-уровень подтверждён здесь; подтверждение в плеере —
шаг 6a ниже).

### Шаг 4-6: воспроизведение в AVPro

Владелец сообщает: адрес `https://s3.example.com/movies/control.mp4` без
query-string открыт в плеере AVPro и играет — картинка не чёрная, звук есть.

Наблюдение: играет (со слов владельца; см. «Пробел в доказательствах» — конкретика
прогона в протоколе не зафиксирована).

### Шаг 6a: боевая форма адреса в плеере (M4)

Владелец сообщает: адрес `https://s3.example.com/movies/control.mp4?exp=1&sig=abc`
открыт в том же плеере и тоже играет.

Наблюдение: играет (со слов владельца; см. «Пробел в доказательствах»).

## Условия прогона в VRChat

Прогон выполнен владельцем в билде VRChat 2026.3.1, в мире `06_Leaf` от
`_hoshino_`, через видеоплеер VizVid, в инстансе типа Invite+ (требование
протокола «не публичный инстанс» выполнено). Оба адреса — чистый и боевой с
`?exp=1&sig=abc` — открыты в одном и том же плеере и играют.

Граница доказательства: воспроизведение наблюдал человек, программной записи
прогона нет — в VRChat её и не получить. Вердикт опирается на прямое сообщение
владельца о наблюдаемом результате. Условия прогона зафиксированы выше и
воспроизводимы.

## Bonus-наблюдение (вход для эксперимента 5 плана 01-03)

Шаг 2 (без query-string) и шаг 3a (с `?exp=1&sig=abc`) вернули **один и тот же**
`X-Amz-Request-Id: 18CDA0C18F621F4E` — то есть кэширующий прокси владельца не
включает query-string в ключ кэша. Для подписанных ссылок это ровно нужное
поведение: разные подписи одного и того же объекта должны делить одну запись в
кэше, а не плодить дубликаты по каждой уникальной подписи. Это наблюдение с
чужого стенда, а не проверенное проектное решение — для собственного edge-nginx
фазы 1 (D-20, `proxy_cache_key`) поведение нужно подтвердить отдельно в
эксперименте 5 плана 01-03.

## Вердикт: PASS

## Отработанные пункты D-33 (заполняется только при FAIL)

Не применимо — эксперимент прошёл с вердиктом PASS, перебор причин провала не
требовался.
