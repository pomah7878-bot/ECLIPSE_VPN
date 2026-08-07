# ECLIPSE Unlimited VPN

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![aiogram](https://img.shields.io/badge/aiogram-3.x-informational)
![Status](https://img.shields.io/badge/status-active-success)

**Готовый Telegram-бот для запуска собственного VPN-бизнеса на базе 3x-UI.** Приём оплаты, автоматическая выдача подписок (VLESS/Reality, XHTTP, Hysteria2), личный кабинет на сайте (Google/Яндекс/VK), AI-консультант с базой знаний, реферальная программа — всё из коробки.

## ✨ Возможности

- 🤖 **Telegram-бот на aiogram 3** — подписки, тарифы, пробный период, промокоды
- 💳 **Приём платежей** — ЮKassa, Telegram Stars, и другие провайдеры через расширяемую систему
- 🌐 **Личный кабинет на сайте** — вход через Google/Яндекс/VK, покупка без установки Telegram
- 🧠 **AI-консультант с RAG** — отвечает на вопросы клиентов, используя реальный профиль и базу знаний поддержки
- 🔌 **Отображение отдельных подключений** — VLESS Reality/XHTTP, Hysteria2 с реальным пингом из браузера клиента
- 🎁 **Реферальная программа** — с таблицей лидеров и настраиваемыми бонусами
- 🛡️ **Защита пробного периода** — Cloudflare Turnstile + rate-limit против ботов
- 🔄 **Автообновления** — ежедневная проверка новых версий с уведомлением админа и обновлением в один клик
- 🧩 **Система кастомных расширений** — безопасное добавление своей логики без изменения ядра
- 📊 **Админ-панель** — управление пользователями, серверами, тарифами, рассылками, статистикой

---

## 🚀 Быстрая установка (новый чистый сервер Ubuntu 24)

Подключитесь к серверу по SSH и выполните одну команду:

```bash
bash <(curl -sL https://raw.githubusercontent.com/pomah7878-bot/ECLIPSE_VPN/main/install.sh)
```

Выберите **1) 🚀 Установка**, введите токен бота (от @BotFather) и ваш Telegram ID — скрипт автоматически:
- обновит системные пакеты
- скачает репозиторий
- создаст виртуальное окружение и установит Python-зависимости
- настроит автозапуск через systemd (`eclipse-vpn.service`)
- запустит бота

### Автоматический запуск без диалогов

```bash
bash <(curl -sL https://raw.githubusercontent.com/pomah7878-bot/ECLIPSE_VPN/main/install.sh) install <BOT_TOKEN> <ADMIN_ID>
```

### Обновление уже установленного бота

```bash
cd /root/EclipseVPN
bash install.sh update
```

---

## 📋 Что нужно подготовить заранее

| Что | Где получить |
|---|---|
| Токен Telegram-бота | [@BotFather](https://t.me/BotFather) → `/newbot` |
| Ваш Telegram ID (для админ-доступа) | [@userinfobot](https://t.me/userinfobot) |
| VPN-сервер с панелью 3x-ui | Любой VPS + [установка 3x-ui](https://github.com/MHSanaei/3x-ui) |
| Домен + SSL (для WebApp/сайта) | Любой регистратор + Let's Encrypt / Certbot |

---

## 🔑 Ручная настройка секретов (после установки)

Установщик создаст `config.py` из ваших ответов, но часть настроек нужно дополнить вручную.

### 1. `secrets.env` — в корне проекта

```bash
cp secrets.env.example secrets.env
nano secrets.env
```

Обязательные для полного функционала:
```env
GROQ_API_KEY=              # console.groq.com — для AI-консультанта
SUPPORT_API_TOKEN=         # любая случайная строка — внутренний токен между ботом и AI-сервисом
```

Для оплаты через ЮKassa (настраивается через админ-панель бота):
- Shop ID и Secret Key — [yookassa.ru](https://yookassa.ru) → личный кабинет → Настройки → API

### 2. OAuth-вход в личный кабинет (Google / Яндекс / VK) — по желанию

Добавьте в `secrets.env`, если нужен вход через соцсети на сайте:
```env
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
YANDEX_OAUTH_CLIENT_ID=
YANDEX_OAUTH_CLIENT_SECRET=
```

Redirect URI при регистрации приложений — **обязательно с вашим доменом**:
```
https://ваш-домен/auth/google/callback
https://ваш-домен/auth/yandex/callback
```

### 3. Nginx + SSL для WebApp и сайта

Пример конфигурации (адаптируйте под свой домен):
```nginx
server {
    server_name ваш-домен;
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/ваш-домен/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ваш-домен/privkey.pem;
}
```

Получить SSL-сертификат:
```bash
apt install certbot python3-certbot-nginx -y
certbot --nginx -d ваш-домен
```

---

## 🛠️ Управление ботом

```bash
systemctl status eclipse-vpn    # статус
systemctl restart eclipse-vpn   # перезапуск
systemctl stop eclipse-vpn      # остановка
journalctl -u eclipse-vpn -f    # логи в реальном времени
```

---

## 🗄️ Ежедневный бэкап базы данных

В репозитории уже есть готовый скрипт `backup_db.sh`:

```bash
chmod +x /root/EclipseVPN/backup_db.sh
crontab -e
```

Добавьте строку:
```
0 4 * * * /root/EclipseVPN/backup_db.sh >> /root/backups/backup.log 2>&1
```

---

## ⚠️ Важно

- `config.py` и `secrets.env` никогда не попадают в git (см. `.gitignore`) — храните их только на сервере
- База данных (`database/vpn_bot.db`) создаётся автоматически при первом запуске
- Перед подключением реальной VPN-панели убедитесь, что 3x-ui доступен и API-профиль (`clients_api` или `xui_api`) настроен корректно


## 💝 Поддержать проект

Если этот проект оказался полезен и вы хотите поддержать его дальнейшую разработку — можно оставить пожертвование любой суммы, какую посчитаете нужной:

**[💳 Поддержать проект через ЮKassa](https://yookassa.ru/my/i/amQkWbtLCsPO/l)**

Спасибо, что пользуетесь ECLIPSE Unlimited! 🙏

---

## История версий

- **1.5** — CI на GitHub Actions: автоматическая проверка синтаксиса и импортов при каждом коммите, до попадания на боевой сервер
- **1.4** — AI-консультант теперь не только отвечает, но и выполняет реальные действия: включает/выключает автопродление, сбрасывает список устройств при упоре в лимит, проверяет и завершает зависшие платежи, безопасно направляет на замену ключа
- **1.3** — тарифы и группы тарифов: лимит устройств и название тарифа видны на карточке ключа, админ может сменить тариф клиенту, автосброс трафика 1-го числа и пробный период теперь настраиваются отдельно для каждой группы тарифов
- **1.2** — экран проверки обновлений показывает актуальные данные без перезапуска
- **1.1** — устранена скрытая ошибка в редком сценарии коллизии кода входа
- **1.0** — первый официальный релиз ECLIPSE Unlimited
