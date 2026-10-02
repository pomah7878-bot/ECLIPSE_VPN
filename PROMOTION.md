# Продвижение проекта

Готовые тексты и чек-лист. Всё, что помечено «в настройках GitHub», делается вручную в интерфейсе репозитория.

## 1. Настройки GitHub (Settings → General и About)

- **Description:** Telegram-бот для продажи VPN: 3x-ui/Xray, оплата, Mini App, сайт-витрина, Android-приложение, whitelabel
- **Website:** ссылка на бот или сайт сервиса
- **Topics:** `vpn` `telegram-bot` `3x-ui` `xray` `vless` `aiogram` `v2ray` `whitelabel` `mini-app` `self-hosted`
- **Social preview** (Settings → General → Social preview): картинка 1280×640 с логотипом и подписью «Telegram-бот для своего VPN-бизнеса»
- Включите **Issues**, **Discussions** и **Private vulnerability reporting** (Settings → Code security)
- Закрепите репозиторий в профиле `pomah7878-bot`

## 2. Что подготовить для README

- 3–5 скриншотов: бот, Mini App, админ-панель, сайт-витрина, Android-приложение (положите в `docs/img/` и вставьте под заголовком)
- Короткая GIF-запись: «покупка ключа за 30 секунд»
- Ссылка на демо-бота

## 3. Площадки

| Площадка | Что публиковать |
|---|---|
| Habr | статья «Как собрать своего VPN-бота на 3x-ui» (черновик ниже) |
| Telegram | каналы про 3x-ui, Xray, self-hosted, VPN-бизнес; свой канал ECLIPSE Unlimited News |
| Reddit | r/selfhosted, r/Telegram: пост на английском с ссылкой и скриншотами |
| GitHub | заявки в списки awesome-selfhosted, awesome-telegram |
| YouTube | видео «Установка VPN-бота за 10 минут» |
| Форумы | профильные русскоязычные форумы по Xray и обходу блокировок — читайте правила раздела |

Не публикуйте токены, IP своих серверов и данные клиентов в скриншотах.

## 4. Черновик статьи для Habr

**Заголовок:** Свой VPN-сервис за вечер: Telegram-бот с оплатой, Mini App и Android-приложением на базе 3x-ui

**План:**
1. Проблема: ручная выдача ключей, оплата в личные сообщения, нет учёта клиентов.
2. Что получилось: бот на aiogram 3, продажа подписок, ЮKassa и Telegram Stars, автоматическая выдача VLESS/Reality, личный кабинет, рефералка, пробный период.
3. Архитектура: бот, 3x-ui как панель, SQLite, systemd, автообновления с проверкой запуска.
4. Установка: одна команда на чистом Ubuntu 24 (раздел «Быстрая установка» в README).
5. Что внутри интересного: защита пробного периода (Cloudflare Turnstile, лимиты), устойчивость к недоступной панели (circuit breaker), устойчивый резервный домен.
6. Whitelabel и лицензия: что бесплатно, что платно (ссылка на LICENSING.md).
7. Планы и ссылка на репозиторий.

## 5. Пост для Reddit (английский, коротко)

> I built an open-source Telegram bot for running your own VPN business on top of 3x-ui (Xray / VLESS Reality): payments, auto-issued subscriptions, Mini App, website storefront, Android app, referrals, multi-server support. One-command install on Ubuntu 24. Feedback welcome: <ссылка>

## 6. SEO сайта-витрины

- На каждой странице уникальные `title` (до 60 символов) и `description` (до 160).
- Страницы-инструкции: «как настроить VPN на iPhone/Android/Windows», «как подключить Happ/v2rayNG» — по таким запросам приходит поиск.
- Подключите Яндекс Вебмастер и Google Search Console, добавьте `sitemap.xml` и `robots.txt`.
- Скорость: сжатые картинки, кэш статики в nginx, HTTPS.
- Внешние ссылки: Telegram-каналы, статьи, каталоги ботов.

## 7. Юридически чувствительное

Реклама VPN-сервисов и инструкции по обходу блокировок в России регулируются законом. Рекламу вне Telegram и профильных технических площадок согласуйте с юристом.

## 8. Файл LICENSE

В репозитории пока нет файла `LICENSE`, а платные функции описаны в LICENSING.md. Выберите тип лицензии (например, source-available вроде Elastic License 2.0 или BSL) и добавьте файл `LICENSE` — это юридическое решение.
