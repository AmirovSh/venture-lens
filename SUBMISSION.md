# Материалы для проверяющего

1. Откройте https://venture-lens.duckdns.org. Гостевой доступ передаётся отдельно. Карточки, источники, аудит и JSON-экспорт доступны без запуска нового исследования.
2. Посмотрите видео, приложенное к сообщению со сдачей задания. Локальная копия предыдущего показа описана в [DEMO_SCRIPT.md](DEMO_SCRIPT.md).
3. Прочитайте [DECISIONS.md](DECISIONS.md), [LIMITATIONS.md](LIMITATIONS.md), [SPEC_COMPLIANCE.md](SPEC_COMPLIANCE.md) и [VERIFICATION.md](VERIFICATION.md).
4. Исходники со всей историей: [GitHub](https://github.com/AmirovSh/venture-lens) (публичный репозиторий, приглашение для чтения не требуется). Альтернатива — `deliverables/venture-lens.bundle`; точный HEAD и проверка — в соседнем `repository-verification.json`.

```sh
git clone venture-lens.bundle venture-lens
cd venture-lens
git log --oneline
```

Установка описана в README.md. Для своего запуска нужны настройки источников и LLM; ключи, сертификаты, пароли, рабочая БД и пользовательские данные не включены.

В одном из готовых публичных исследований 10 карточек. Две допущены к рейтингу, восемь требуют уточнения. Это сохранённый пример. В новом исследовании рынок задаёт пользователь, а количество гипотез определяет адаптивный поиск. Функции интерфейса описаны в [DECISION_WORKSPACE.md](DECISION_WORKSPACE.md) и [PRODUCT_CONTROLS.md](PRODUCT_CONTROLS.md).

Репозиторий опубликован в GitHub, облачные проверки запускаются в [Actions](https://github.com/AmirovSh/venture-lens/actions). Argo CD читает `master` этого репозитория напрямую по HTTPS; конфигурация Application хранится в `deploy/argocd/demo.yaml`. Видео и bundle — отдельные материалы, автоматически в новом клоне не появятся. Повторная сборка передачи: `python scripts/prepare_submission.py`.
