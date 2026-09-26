Ты работаешь над репозиторием Forge.

Текущая версия Forge — CLI-утилита вокруг Docker, которая умеет собирать контейнер, запускать его, проверять health check и переключать трафик через Traefik. Это хороший прототип, но архитектурно это ещё не полноценная deployment platform.

Твоя задача — НЕ просто добавить новые команды и НЕ превращать существующий CLI в огромный скрипт.

Нужно изменить архитектуру Forge так, чтобы это был настоящий self-hosted deployment platform для одного сервера, который в дальнейшем можно расширить до нескольких worker nodes.

Главная идея:

CLI → HTTP API → Control Plane → Deployment Engine → Runtime/Docker → Traefik

CLI должен стать тонким клиентом API, а не местом, где находится бизнес-логика.

### 1. Новая архитектура

Раздели систему на следующие логические компоненты:

forge/
api/
core/
deployments/
runtime/
scheduler/
storage/
proxy/
config/
events/
logs/
cli/

Нужны как минимум:

* API server
* persistent state storage
* deployment manager
* reconciliation loop
* runtime abstraction
* proxy abstraction
* event system
* application/deployment models
* CLI client

Не допускай ситуации, когда CLI напрямую реализует deployment lifecycle.

### 2. Forge должен стать daemon/server

Добавь постоянно работающий Forge server.

Он должен:

* запускаться как long-running process;
* принимать HTTP API requests;
* хранить состояние приложений и deployments;
* запускать deployments как background jobs;
* восстанавливать состояние после рестарта;
* регулярно выполнять reconciliation;
* обнаруживать containers, которые существуют в Docker, но отсутствуют в ожидаемом состоянии Forge;
* обрабатывать failed deployments;
* не терять deployment state после падения процесса.

Для первой полноценной версии поддерживай только single-node deployment.

Не добавляй Kubernetes.

Не создавай распределённую систему ради красивой архитектуры.

### 3. Persistent state

Добавь SQLite как первичное хранилище.

Минимальные сущности:

Application
Deployment
DeploymentRevision
Container
Event
EnvironmentVariable

Пример состояния deployment:

PENDING
BUILDING
STARTING
HEALTH_CHECKING
ACTIVE
STOPPING
STOPPED
FAILED
ROLLED_BACK

Состояние должно быть явным state machine, а не набором boolean-флагов.

Храни:

* application id
* application name
* desired configuration
* current revision
* deployment status
* timestamps
* image name
* container id
* error information
* health status
* domain
* port
* deployment history

### 4. Deployment Engine

Вынеси deployment lifecycle в отдельный компонент.

Пример:

create deployment
→ validate config
→ build image
→ create candidate
→ attach network
→ configure proxy labels
→ start container
→ health check
→ promote candidate
→ gracefully stop previous revision
→ cleanup old resources

Каждый шаг должен иметь явное состояние.

Deployment должен быть idempotent.

Повторный запрос на тот же deployment не должен случайно создать хаос из контейнеров.

Добавь concurrency protection, чтобы два deployment одного приложения не могли одновременно менять production state.

### 5. Reconciliation

Это одна из главных целей проекта.

Forge должен иметь reconciliation loop.

Он сравнивает:

desired state

с

actual state

и пытается привести систему к desired state.

Примеры:

* Forge думает, что deployment ACTIVE, но container умер;
* container существует, но Forge не знает о нём;
* candidate остался после failed deployment;
* старый container не был удалён после rollback;
* Forge был перезапущен во время deployment.

Reconciler должен корректировать такие состояния.

Не делай его бесконечным циклом с хаотичными sleep.

Сделай отдельный компонент с контролируемым interval и cancellation.

### 6. Runtime abstraction

Не привязывай domain logic напрямую к `subprocess`.

Создай интерфейс примерно такого уровня:

Runtime
build_image()
create_container()
start_container()
stop_container()
remove_container()
inspect_container()
exec()
logs()

DockerRuntime будет первой реализацией.

Бизнес-логика deployment engine не должна знать, что внутри используется именно Docker CLI.

Создай FakeRuntime для unit tests.

### 7. Proxy abstraction

То же самое сделай для Traefik.

Deployment engine не должен содержать множество конкретных Traefik commands.

Создай Proxy abstraction.

Например:

configure_service()
remove_service()
promote_revision()

Первая реализация — Traefik через Docker labels.

### 8. API

Сделай нормальный REST API.

Минимальный набор:

GET    /health
GET    /api/v1/applications
POST   /api/v1/applications
GET    /api/v1/applications/{id}
DELETE /api/v1/applications/{id}

GET    /api/v1/applications/{id}/deployments
POST   /api/v1/applications/{id}/deployments
GET    /api/v1/deployments/{id}

POST   /api/v1/deployments/{id}/rollback

GET    /api/v1/applications/{id}/events
GET    /api/v1/applications/{id}/logs

API должен возвращать структурированные JSON responses.

Ошибки тоже должны быть структурированными.

Не смешивай HTTP logic с deployment logic.

### 9. Background jobs

Deployment не должен выполняться внутри HTTP request до полного завершения.

POST /deployments должен:

1. создать deployment record;
2. поставить deployment в очередь;
3. вернуть deployment id;
4. worker выполняет deployment asynchronously.

Добавь простой internal job queue.

Не используй Celery, RabbitMQ или Kafka на этом этапе.

Нужна простая и понятная архитектура.

### 10. Events

Каждое важное изменение состояния должно порождать Event.

Например:

deployment.created
deployment.build_started
deployment.build_succeeded
deployment.container_started
deployment.health_check_passed
deployment.promoted
deployment.failed
deployment.rollback_started
deployment.rollback_completed

Это позволит потом легко сделать UI и streaming API.

### 11. Logs

Сделай возможность получать:

* application logs;
* deployment logs;
* Forge server logs.

Не обязательно строить полноценный log aggregation system.

На первом этапе достаточно корректного streaming/read API поверх Docker logs и Forge events.

### 12. Configuration

Сохрани forge.json / forge.yaml, но сделай конфигурацию полноценной.

Пример:

{
"app_name": "my-service",
"domain": "my-service.localhost",
"container_port": 8000,
"health_check": {
"path": "/health",
"timeout": 5,
"retries": 10
},
"deployment": {
"strategy": "blue_green",
"graceful_shutdown": 10
}
}

Конфигурация должна валидироваться до deployment.

Ошибки конфигурации должны быть понятными.

### 13. CLI

CLI теперь должен быть клиентом API.

Примеры:

forge server
forge app create
forge app list
forge deploy ./my-app
forge deployments
forge logs my-app
forge rollback my-app
forge status my-app

CLI не должен самостоятельно управлять Docker lifecycle.

Он отправляет HTTP requests Forge server.

### 14. Recovery

Это обязательная часть проекта.

Проверь сценарии:

* Forge restart during deployment;
* Docker restart;
* container crash;
* health check failure;
* failed image build;
* failed candidate startup;
* Forge crash after promotion;
* orphaned container;
* duplicate deployment request.

Система должна после restart прочитать SQLite state и продолжить reconciliation.

### 15. Security & Threat Model (Baseline Hardening)

> **Критический архитектурный дисклеймер**:
> Меры ниже обеспечивают **baseline hardening** (базовое ужесточение конфигурации и защиту от тривиальных атак и ошибок оператора), но **НЕ являются гарантией безопасности (security guarantee)** или изоляцией уровня виртуализации/песочницы.
> Контейнеры Docker делят одно ядро хоста. Запуск произвольного недоверенного кода без microVM (Kata Containers, Firecracker) или gVisor несёт остаточный риск побега из контейнера на уровне уязвимостей ядра ОС. Forge ориентирован на доверенное или условно-доверенное окружение одной ноды.

#### 15.1 Модель угроз (Threat Model)

Система разделяет 5 акторов с разным уровнем доверия:

1. **Trusted Operator (Доверенный оператор)**:
   * Администратор хоста, запускающий процесс `forge server`. Имеет права на хосте (root / docker group).
   * *Угрозы*: Ошибки конфигурации, случайный дамп секретов в консоль/артефакты, непреднамеренное удаление рабочего окружения.
2. **Deployed Application (Развёртываемое приложение)**:
   * Пользовательский код внутри контейнеров. Считается потенциально враждебным или содержащим уязвимости.
   * *Угрозы*: Побег на хост через Docker socket, потребление 100% RAM/CPU/PIDs (DoS хоста), чтение данных других контейнеров в сети, сканирование облачных метаданных хоста (169.254.169.254), инъекции в правила роутинга Traefik.
3. **Remote API Client (Клиент API)**:
   * Внешний субъект, отправляющий HTTP-запросы к API Control Plane.
   * *Угрозы*: Подбор токенов (брутфорс, timing attacks), Path Traversal при передаче путей/манифестов, SSRF через манипуляции с health-check URL, DoS API сервера флудом запросов.
4. **Docker Daemon**:
   * Привилегированная служба хоста с root-эквивалентными правами.
   * *Граница доверия*: Компрометация доступа к Docker socket означает полную компрометацию хоста.
5. **Traefik Ingress**:
   * Входной реверс-прокси, читающий события Docker и маршрутизирующий входящий трафик.
   * *Угрозы*: Некорректные или вредоносные лейблы контейнеров, способные сломать конфигурацию роутера или перехватить чужие домены.

#### 15.2 Границы доверия и привилегии (Trust Boundaries)

* **Привилегированная зона (Privileged)**:
  * `Forge Daemon`: Работает на хосте, имеет доступ к SQLite и Unix-сокету Docker (`/var/run/docker.sock`).
  * `Docker Daemon`: Полный root на хосте.
  * `Traefik`: Имеет доступ к чтению сокета Docker и слушает привилегированный порт 80 на хосте.
* **Непривилегированная зона (Unprivileged)**:
  * `Deployed Applications`: **Никаких привилегий**. Полный запрет доступа к Docker socket, запрет монтирования хостовой ФС, запрет хостовых пространств имён.
  * `API Clients`: Доступ строго ограничен endpoints API после валидации Bearer токена.
* **Правило пересечения границ**: Любые данные из непривилегированной зоны (API payloads, `forge.json`, `.env`, имена приложений) считаются недоверенными и проходят строгую валидацию по белым спискам до передачи в Control Plane.

#### 15.3 Архитектура работы с секретами (Zero-Leak Data Path)

Не полагаться исключительно на log scrubber. Секреты должны быть исключены из потоков данных архитектурно:

1. **Запрет передачи секретов через аргументы командной строки**:
   * Не передавать переменные окружения через аргументы `docker run -e KEY=VAL` (они видны в `ps aux` и `docker inspect` любому непривилегированному пользователю хоста).
   * Передавать окружение в контейнер через временный `--env-file` с правами доступа `0600`, который удаляется сразу после создания контейнера, либо через внутренний механизм монтирования.
2. **Исключение секретов из событий (Events)**:
   * Сущность `Event` и таблица `events` **никогда** не хранят значения секретов.
   * В payload событий разрешено сохранять только метаданные ключей (например: `{"updated_keys": ["DATABASE_URL"]}`).
3. **Исключение секретов из стандартных API ответов**:
   * `GET /api/v1/applications/{id}` возвращает имена переменных окружения и маскированный статус: `{"key": "DB_PASS", "is_set": true}`, но **не** их открытые значения.
4. **Изоляция постоянных логов (Persistent Logs)**:
   * Логи деплоя фиксируют только stdout/stderr процессов сборки и контейнеров. Forge Control Plane не пишет словарь env vars в лог-файлы.
   * Защитный Log Scrubber в `forge/logs/` выступает лишь как вторичный барьер (defense-in-depth) на случай, если приложение само напечатает секрет в stdout при ошибке.

#### 15.4 Поверхность безопасности Docker (Docker Security Surface)

При создании контейнера приложения через `Runtime.create_container()` жёстко фиксируются следующие флаги:

* `privileged`: Всегда `False` (запрет запуска в привилегированном режиме).
* `capabilities`: Дропать все системные capabilities (`--cap-drop=ALL`), точечно возвращая только необходимые (`--cap-add=NET_BIND_SERVICE`).
* `devices`: Полный запрет монтирования физических устройств хоста (`--device` заблокирован).
* `network`: Только виртуальный мост `forge-net`. Флаг `--net=host` запрещён архитектурно.
* `Docker socket`: Полный запрет монтирования `/var/run/docker.sock` в пользовательские контейнеры.
* `namespaces`: Изолированные пространства имён (запрет `--pid=host`, `--ipc=host`, `--uts=host`).
* `bind mounts`: Запрет произвольного монтирования путей хоста. Приложениям доступны только именованные тома Docker или изолированная рабочая директория.
* `resource limits`: Дефолтные защитные квоты от DoS хоста на каждом контейнере:
  * память: `--memory=512m` (настраивается в манифесте);
  * процессор: `--cpus=1.0`;
  * лимит процессов: `--pids-limit=150` (защита от fork-бомб).

#### 15.5 Политика перезапусков и согласование со State Machine (Restart Policy)

* **Проблема**: Docker restart policy (`--restart=always`, `--restart=on-failure`) конфликтует с циклом деплоя Forge. Если candidate контейнер падает при старте или health-check, Docker начинает циклически его перезапускать, мешая Forge зафиксировать сбой и удалить его.
* **Архитектурное решение**:
  1. На этапах `STARTING` и `HEALTH_CHECKING` candidate запускается **строго с `--restart=no`**. Только State Machine Forge имеет право решать, жив контейнер или его нужно снести.
  2. Только после успешного прохождения health check и перехода в `ACTIVE` контейнер может обновляться на политику `unless-stopped`.
  3. Основной механизм обеспечения доступности — `Reconciler` Forge: если активный контейнер падает в Docker, Reconciler обнаруживает несоответствие (`actual: stopped` vs `desired: active`) и инициирует восстановительный деплой.

#### 15.6 Защита от SSRF в Health Checks

* **Вектор атаки**: Манипуляция полем `health_check.path` в `forge.json` (например: `http://169.254.169.254/latest/meta-data/` для кражи метаданных облачного провайдера или `@internal-service:6379` для атак на локальные порты).
* **Архитектурные требования**:
  1. **Строгая валидация пути**: `path` должен быть относительным URI-путем, начинающимся со слеша, валидируемым по regex: `^/[a-zA-Z0-9_\-\./]*$`.
  2. **Запрет схем и хостов**: Запрещены символы `:`, `@`, `?`, схемы `http://`, `https://`, переводы строк `\r`, `\n`.
  3. **Локализация цели**: Запрос формируется строго на `http://127.0.0.1:{container_port}{path}` и выполняется **внутри** контекста контейнера через `Runtime.exec()`, исключая доступ к loopback-интерфейсу хост-машины Forge.

#### 15.7 Маппинг требований безопасности по фазам и тестам

| Требование безопасности | Фаза реализации | Тип теста и проверяемый сценарий |
| :--- | :--- | :--- |
| **API Auth & Timing-Safe Check** | Phase 5 (API) | Unit: `test_api_auth_invalid_token_rejected`, `test_api_timing_safe_compare` |
| **Localhost Bind Only** | Phase 1 (Server) | Unit/Integration: `test_server_binds_strictly_to_127_0_0_1` |
| **App Name & Domain Regex** | Phase 1 (Models/Config) | Unit: `test_invalid_app_name_injection_rejected`, `test_traefik_rule_injection_prevented` |
| **Health Check SSRF Protection** | Phase 1 (Config) | Unit: `test_health_check_path_ssrf_and_scheme_rejected` |
| **No Secrets in Events** | Phase 1 (Models/Storage) | Unit: `test_event_payload_does_not_contain_secret_values` |
| **No Secrets in API Responses**| Phase 5 (API) | Unit: `test_get_application_masks_environment_secrets` |
| **Env File Isolation (No CLI args)** | Phase 4 (Runtime) | Unit: `test_docker_run_uses_env_file_not_command_line_args` |
| **Docker Hardening Flags** | Phase 4 (Runtime) | Unit: `test_create_container_applies_security_flags` (drop caps, no host net, pids limit, no privileged) |
| **Candidate Restart=No** | Phase 2 (Deployment Engine)| Unit: `test_candidate_container_starts_with_restart_no` |
| **Docker Socket Mount Denied** | Phase 4 (Runtime) | Integration: `test_application_container_cannot_access_docker_socket` |

### 16. Testing

Тесты должны быть архитектурными, а не просто ради coverage.

Нужны unit tests для:

* state machine;
* deployment transitions;
* reconciliation;
* configuration validation;
* API;
* runtime adapter;
* proxy adapter;
* concurrency;
* rollback.

Отдельно сделай integration tests с реальным Docker.

Unit tests не должны требовать Docker.

Используй FakeRuntime/FakeProxy там, где это необходимо.

### 17. Что НЕ делать

Не добавляй:

* Kubernetes;
* Redis;
* Kafka;
* Celery;
* microservices;
* distributed consensus;
* multi-region;
* service mesh;
* Terraform;
* fake cloud abstractions;
* бессмысленный frontend;
* десятки настроек ради объёма.

Не превращай проект в набор abstraction classes без реальной необходимости.

Не переписывай всё вслепую.

Не сохраняй старую архитектуру только ради обратной совместимости, если она мешает новой модели.

### 18. Важное требование к качеству

README сейчас местами описывает Forge более масштабно, чем позволяет его реальная архитектура.

После изменений документация должна описывать только реально существующее поведение.

Не используй слова вроде:

"guarantees"
"production-ready"
"fault tolerant"
"distributed"
"zero downtime"

если реализация фактически этого не обеспечивает.

Каждое сильное утверждение в README должно быть подтверждено реализацией или тестом.

### 19. Порядок работы

Не пытайся написать всё одним огромным патчем.

Сначала:

1. проанализируй существующий код;
2. опиши текущую архитектуру;
3. найди точки, которые нужно сохранить;
4. предложи целевую структуру;
5. составь migration plan;
6. создай ARCHITECTURE.md;
7. только после этого начинай реализацию.

Затем реализовывай по вертикальным срезам:

Phase 1:
server + SQLite + models

Phase 2:
deployment service + state machine

Phase 3:
background jobs + reconciliation

Phase 4:
runtime/proxy abstractions

Phase 5:
REST API

Phase 6:
CLI as API client

Phase 7:
recovery + rollback

Phase 8:
integration tests

После каждого phase:

* запускай tests;
* проверяй imports;
* проверяй реальные Docker сценарии там, где возможно;
* исправляй архитектурные проблемы, обнаруженные при интеграции.

### 20. Definition of Done

Forge должен позволять сделать примерно следующее:

1. Запустить Forge server.
2. Создать application.
3. Передать Forge проект с Dockerfile.
4. Forge создаёт deployment record.
5. Deployment выполняется в background.
6. Image build происходит через runtime.
7. Candidate container запускается.
8. Выполняется health check.
9. Candidate становится active.
10. Старый revision корректно завершается.
11. Все изменения видны через API/events.
12. CLI получает эти данные через API.
13. При перезапуске Forge состояние восстанавливается из SQLite.
14. При падении container reconciliation обнаруживает проблему.
15. Rollback возвращает предыдущую revision.
16. Unit tests проходят без Docker.
17. Integration tests проверяют реальные Docker операции.

Итоговая цель:

Forge должен ощущаться не как "Python script that runs Docker commands", а как небольшой, но настоящий control plane для deployment lifecycle.

Приоритет:

correctness > clear architecture > recovery > testability > features.

Не увеличивай размер проекта искусственно. Каждый новый компонент должен существовать потому, что он решает конкретную архитектурную проблему.
