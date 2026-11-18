# Материалы для проверяющего

2. Посмотрите `deliverables/venture-lens-demo.mp4` — 2:50,5, русская озвучка и субтитры.
3. Прочитайте [DECISIONS.md](DECISIONS.md), [LIMITATIONS.md](LIMITATIONS.md), [SPEC_COMPLIANCE.md](SPEC_COMPLIANCE.md) и [VERIFICATION.md](VERIFICATION.md).
4. Исходники со всей историей: `deliverables/venture-lens.bundle`. Результат проверки и точный HEAD — в соседнем `repository-verification.json`.

```sh
git clone venture-lens.bundle venture-lens
cd venture-lens
git log --oneline
```

Установка описана в README.md. Для своего запуска нужны настройки источников и LLM; ключи, сертификаты, пароли, рабочая БД и пользовательские данные не включены.

Последнее исследование: 10 карточек, 2 допущены, 8 требуют уточнения, технических ошибок и подтверждённых отказов нет. Индексы 19/100 и 64/100 не являются обещанием успеха. Видео отражает предыдущий снимок интерфейса; новые возможности описаны в DECISION_WORKSPACE.md.

GitHub-публикация пока не выполнена из-за недействительной локальной авторизации. Сайт и Argo CD используют частное Git-зеркало VPS. Видео и bundle — отдельные материалы, автоматически в новом клоне не появятся. Повторная сборка передачи: `python scripts/prepare_submission.py`.
