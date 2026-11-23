# Security Policy

Пожалуйста, **не открывайте публичный issue** для уязвимостей.
Используйте приватный отчёт GitHub:
`Security` → `Report a vulnerability`
(https://docs.github.com/ru/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).

## Поддерживаемые интерпретаторы и политика зависимостей

Пакет объявляет `requires-python = ">=3.10"`. Python 3.10 вышел из фазы
upstream-поддержки (октябрь 2026), поэтому пол поддерживается только до
переезда на 3.11+, а не как долгосрочное обязательство.

`uv.lock` содержит две ветки `pillow`: 12.3.0 для `python_version < 3.11` и
13.0.0 для остальных. Пропатченного релиза ветки 12.x с исправлениями
CVE-2026-… нет, поэтому открытые алерты Dependabot для `pillow<3.11`
(dependabot #* в `https://github.com/Alexkkkkk/mount-file/security/dependabot`)
не закрываются обновлением версии, а снимаются только поднятием пола Python.

Политика:

1. Алерты не закрываются «насовсем». Допустимы только `dismiss` с причиной и
   датой пересмотра, зафиксированными в описании алерта.
2. Приоритет исправления: поднятие `requires-python` до 3.11 и удаление ветки
   `pillow<3.11` из `uv.lock`. Это изменение входит в релиз, а не в патч.
3. Пока пол не поднят, `pip-audit` в CI остаётся обязательным контекстом
   (`pip-audit` в required checks ветки `main`) — он ловит новые advisory, а не
   только известный набор.
4. `requirements-win7.txt` — исторический манифест, не поддерживается и не
   устанавливается; актуальный пол версии в нём указан как `>=3.10`.
