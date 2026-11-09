# GitHub MCP Server

Подключение официального [`github/github-mcp-server`](https://github.com/github/github-mcp-server)
к этому репозиторию: правки кода, чтение issue/PR и просмотр CI — из агента,
без копирования логов в чат.

## Что где настраивается

| Где | Что это | Как попадает в репозиторий |
|---|---|---|
| `.vscode/mcp.json` | GitHub Copilot в VS Code, локальные агенты | файл в репозитории (этот каталог) |
| Settings → Copilot → **MCP servers** | Copilot cloud agent, automations, code review | только через веб-интерфейс GitHub, файлом не задаётся |
| `~/.config/...` клиента | Claude Desktop / Cursor / Codex и др. | локально у каждого разработчика |

## 1. VS Code

Файл [`.vscode/mcp.json`](https://github.com/Alexkkkkk/mount-file/blob/main/.vscode/mcp.json) уже в репозитории. Он объявляет
два сервера на одном remote-эндпоинте `https://api.githubcopilot.com/mcp/`:

* `github` — только чтение (`X-MCP-Readonly: true`), тулсеты
  `context, repos, issues, pull_requests, actions, code_security, secret_protection`;
* `github-write` — запись (`X-MCP-Readonly: false`), тулсеты
  `context, repos, issues, pull_requests`.

Токен не хранится в файле: он запрашивается один раз при первом запуске
сервера (`inputs[].type: promptString`, `password: true`) и подставляется в
заголовок `Authorization: Bearer ${input:github_mcp_pat}`.

Проверка: палитра команд → **MCP: List Servers** → `github` → **Start**, затем
в Copilot Chat переключиться в режим Agent и вызвать `get_me`.

### Локальный сервер через Docker

Если нужен не remote-эндпоинт, а локальный процесс:

```bash
docker run -i --rm \
  -e GITHUB_PERSONAL_ACCESS_TOKEN=<token> \
  -e GITHUB_TOOLSETS="context,repos,issues,pull_requests" \
  -e GITHUB_READ_ONLY=1 \
  ghcr.io/github/github-mcp-server
```

Образ: `ghcr.io/github/github-mcp-server`. Для stdio-подключения в VS Code
используется `"type": "stdio"` и эта же команда в `command`/`args`.

## 2. Copilot cloud agent (Settings → Copilot → MCP servers)

Настройка живёт в настройках репозитория, не в файле:

1. Страница репозитория → **Settings**.
2. В сайдбаре «Code, planning, and automation» → **Copilot** → **MCP servers**.
3. В поле «MCP configuration» вставить JSON (схема — `mcpServers`, для remote
   сервера обязательны `type` и `tools`):

```json
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "Authorization": "Bearer $COPILOT_MCP_GITHUB_PAT",
        "X-MCP-Toolsets": "context,repos,issues,pull_requests",
        "X-MCP-Readonly": "true"
      },
      "tools": ["*"]
    }
  }
}
```

4. **Save MCP configuration** — GitHub валидирует синтаксис при сохранении.

### Секрет

Секреты и переменные агента видны конфигурации только с префиксом
`COPILOT_MCP_`. Создать: **Settings** → **Secrets and variables** → **Agents**
→ новый секрет с именем `COPILOT_MCP_GITHUB_PAT`. В JSON он подставляется как
`$COPILOT_MCP_GITHUB_PAT`, `${COPILOT_MCP_GITHUB_PAT}` или
`${COPILOT_MCP_GITHUB_PAT:-fallback}`.

Список `tools` держите read-only: агент вызывает эти инструменты
самостоятельно, без подтверждения. `"tools": ["*"]` включает всё.

## 3. Другие клиенты (Claude Desktop, Cursor, Codex, Copilot CLI)

У каждого клиента свой конфиг; официальные сниппеты —
[installation-guides](https://github.com/github/github-mcp-server/tree/main/docs/installation-guides).
Портативный формат использует ключ `mcpServers` (а не `servers`, как в VS Code) —
не переносите блок между ними без правки ключа.

## Тулсеты

Имена тулсетов (из README сервера): `context`, `actions`, `code_quality`,
`code_security`, `copilot`, `copilot_issue_intents`, `dependabot`,
`discussions`, `gists`, `git`, `governance`, `issues`, `labels`,
`notifications`, `orgs`, `projects`, `pull_requests`, `repos`,
`secret_protection`, `security_advisories`, `stargazers`, `users`.
Только в remote-сервере: `copilot_spaces`, `github_support_docs_search`.

`default` = `context` + `repos` + `issues` + `pull_requests` + `users`.
`all` = все тулсеты. Добавление к дефолту: `GITHUB_TOOLSETS="default,stargazers"`.

## Переменные окружения локального сервера

| Переменная | Назначение |
|---|---|
| `GITHUB_PERSONAL_ACCESS_TOKEN` | PAT, имеет приоритет над OAuth |
| `GITHUB_TOOLSETS` | список тулсетов, приоритетнее флага `--toolsets` |
| `GITHUB_TOOLS` | отдельные инструменты, дополняет тулсеты |
| `GITHUB_READ_ONLY=1` | запрет всех не-read-only инструментов |
| `GITHUB_HOST` | GitHub Enterprise Server / ghe.com |
| `GITHUB_OAUTH_CALLBACK_PORT` | фиксированный порт OAuth-callback (Docker) |

## Безопасность

* PAT в репозиторий не коммитится: в `.vscode/mcp.json` только ссылка на
  `input`, в настройках Copilot — ссылка на `COPILOT_MCP_*`.
* Для чтения кода и CI достаточно fine-grained токена с `Contents: Read`,
  `Issues: Read`, `Pull requests: Read`, `Actions: Read`, `Metadata: Read`.
* Права на запись выдавайте отдельному серверу (`github-write`) и только когда
  они действительно нужны.
