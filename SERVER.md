# Перенос «Нарядчика» на сервер (Linux, например Ubuntu)

Зачем: бот работает 24/7 и не зависит от того, включён ли ваш компьютер.
Нужен недорогой VPS (1 ГБ памяти хватит) и доступ по SSH.

## Шаг 1. Остановите бота на компьютере
Закройте чёрное окно бота.

## Шаг 2. Подготовьте сервер (один раз)
```
sudo apt update && sudo apt install -y python3 python3-venv unzip
sudo adduser --system --group --home /opt/naryadchik naryadchik
```

## Шаг 3. Загрузите файлы
Скопируйте на сервер папку проекта целиком, вместе с **.env**, **naryadchik.sqlite3** и папкой **backups**
(например, программой WinSCP или командой `scp`) в `/opt/naryadchik`.
Потом:
```
sudo chown -R naryadchik:naryadchik /opt/naryadchik
cd /opt/naryadchik
sudo -u naryadchik python3 -m venv .venv
sudo -u naryadchik .venv/bin/pip install -r requirements.txt
```

## Шаг 4. Проверьте .env
Впишите `TIMEZONE=` вашего региона (например `Asia/Yekaterinburg`), иначе время напоминаний и копий будет по часам сервера.

## Шаг 5. Включите автозапуск
```
sudo cp deploy/naryadchik.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now naryadchik
```
Проверить: `sudo systemctl status naryadchik` (должно быть «active (running)»).
Логи: `sudo journalctl -u naryadchik -f`.

## Обновление бота
Остановите: `sudo systemctl stop naryadchik`, замените файлы `.py`, запустите: `sudo systemctl start naryadchik`.
Файлы `.env` и `naryadchik.sqlite3` не трогайте.

## Восстановление из копии
Остановите бота, возьмите нужный файл из папки `backups`, скопируйте его на место `naryadchik.sqlite3`, запустите бота.

Важно: бот должен работать только в ОДНОМ месте. Если запущен и на компьютере, и на сервере, Telegram выдаст ошибку.
