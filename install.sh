#!/bin/bash

# ECLIPSE Unlimited VPN — скрипт установки и управления
# Запуск: bash <(curl -sL https://raw.githubusercontent.com/pomah7878-bot/ECLIPSE_VPN/main/install.sh)
# 
# === АВТОМАТИЧЕСКИЙ ЗАПУСК (БЕЗ ДИАЛОГОВ) ===
#
# 1. Запуск прямо с GitHub (для чистой установки или если папки ещё нет):
# bash <(curl -sL https://raw.githubusercontent.com/pomah7878-bot/ECLIPSE_VPN/main/install.sh) install <BOT_TOKEN> <ADMIN_ID>
# bash <(curl -sL https://raw.githubusercontent.com/pomah7878-bot/ECLIPSE_VPN/main/install.sh) update [COMMIT_OR_BRANCH]
# bash <(curl -sL https://raw.githubusercontent.com/pomah7878-bot/ECLIPSE_VPN/main/install.sh) reset [COMMIT_OR_BRANCH]
#
# 2. Локальный запуск (если репозиторий уже установлен и нужно просто обновить/сбросить):
# bash install.sh update [COMMIT_OR_BRANCH]
# bash install.sh reset [COMMIT_OR_BRANCH]

set -e

INSTALL_DIR="/root/EclipseVPN"
REPO_URL="https://github.com/pomah7878-bot/ECLIPSE_VPN.git"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_FILE="eclipse-vpn.service"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_header() {
    echo -e "\n${CYAN}========================================${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}========================================${NC}\n"
}

print_ok() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_err() {
    echo -e "${RED}[✗]${NC} $1"
}

# Запрос настроек у пользователя
ask_config() {
    print_header "Настройка конфигурации"

    if [ "$AUTO_MODE" = "1" ]; then
        NEED_WRITE_CONFIG=1
        print_ok "Автоматический режим: используем переданные параметры"
        return 0
    fi

    if [ -f "$INSTALL_DIR/config.py" ]; then
        echo -e "${YELLOW}Обнаружен существующий config.py${NC}"
        read -p "Использовать существующие настройки? (Y/n): " use_existing
        use_existing=${use_existing:-Y}
        if [[ "$use_existing" =~ ^[YyДд]$ ]]; then
            print_ok "Используем существующий config.py"
            return 0
        fi
    fi

    echo ""
    echo -e "${CYAN}Введите данные для настройки бота:${NC}"
    echo ""

    while true; do
        read -p "BOT_TOKEN (от @BotFather): " bot_token
        if [ -n "$bot_token" ]; then
            break
        fi
        print_err "BOT_TOKEN не может быть пустым!"
    done

    while true; do
        read -p "ADMIN_IDS (ваш Telegram ID): " admin_id
        if [ -n "$admin_id" ] && [[ "$admin_id" =~ ^[0-9]+$ ]]; then
            break
        fi
        print_err "ADMIN_IDS должен быть числом!"
    done

    BOT_TOKEN="$bot_token"
    ADMIN_ID="$admin_id"
    NEED_WRITE_CONFIG=1
    print_ok "Данные получены"

    echo ""
    echo -e "${CYAN}Необязательные настройки — можно пропустить (просто нажмите Enter)${NC}"
    echo -e "${CYAN}и настроить позже прямо в боте: Админ-панель → Настройки бота → 🌐 Интеграции${NC}"
    echo ""

    read -p "Домен сайта, например https://мой-домен.ru (Enter — пропустить): " webapp_url
    WEBAPP_URL_INPUT="$webapp_url"

    read -p "Ключ AI Groq, console.groq.com (Enter — пропустить): " groq_key
    GROQ_KEY_INPUT="$groq_key"

    read -p "Ключ веб-поиска Tavily, tavily.com (Enter — пропустить): " tavily_key
    TAVILY_KEY_INPUT="$tavily_key"

    echo ""
    read -p "Настроить вход через Google/Яндекс/VK сейчас? (y/N): " setup_oauth
    GOOGLE_CLIENT_ID_INPUT=""
    GOOGLE_CLIENT_SECRET_INPUT=""
    YANDEX_CLIENT_ID_INPUT=""
    YANDEX_CLIENT_SECRET_INPUT=""
    VK_CLIENT_ID_INPUT=""
    VK_CLIENT_SECRET_INPUT=""
    if [[ "$setup_oauth" =~ ^[YyДд]$ ]]; then
        echo -e "${CYAN}Для каждого провайдера можно пропустить обе строки (Enter, Enter)${NC}"
        echo ""
        echo "-- Google --"
        read -p "  Client ID (Enter — пропустить): " GOOGLE_CLIENT_ID_INPUT
        if [ -n "$GOOGLE_CLIENT_ID_INPUT" ]; then
            read -p "  Client Secret: " GOOGLE_CLIENT_SECRET_INPUT
        fi
        echo "-- Яндекс --"
        read -p "  Client ID (Enter — пропустить): " YANDEX_CLIENT_ID_INPUT
        if [ -n "$YANDEX_CLIENT_ID_INPUT" ]; then
            read -p "  Client Secret: " YANDEX_CLIENT_SECRET_INPUT
        fi
        echo "-- VK --"
        read -p "  Client ID (Enter — пропустить): " VK_CLIENT_ID_INPUT
        if [ -n "$VK_CLIENT_ID_INPUT" ]; then
            read -p "  Client Secret: " VK_CLIENT_SECRET_INPUT
        fi
    fi

}

# Создание/обновление config.py
write_config() {
    if [ "$NEED_WRITE_CONFIG" != "1" ]; then
        return 0
    fi

    cp "$INSTALL_DIR/config.py.example" "$INSTALL_DIR/config.py"

    sed -i "s|\"ВАШ_ТОКЕН_БОТА\"|\"$BOT_TOKEN\"|g" "$INSTALL_DIR/config.py"
    sed -i "s|12345678|$ADMIN_ID|g" "$INSTALL_DIR/config.py"

    if [ -n "$WEBAPP_URL_INPUT" ]; then
        sed -i "s|\"https://ваш-домен.example.com\"|\"$WEBAPP_URL_INPUT\"|g" "$INSTALL_DIR/config.py"
        print_ok "Домен сайта сохранён в config.py"
    else
        print_warn "Домен сайта пропущен — настройте позже в боте (Интеграции) или в config.py"
    fi

    print_ok "config.py создан с вашими настройками"

    # secrets.env — генерируем из шаблона и заполняем то, что ввёл пользователь
    if [ ! -f "$INSTALL_DIR/secrets.env" ]; then
        cp "$INSTALL_DIR/secrets.env.example" "$INSTALL_DIR/secrets.env"
    fi

    if [ -n "$GROQ_KEY_INPUT" ]; then
        sed -i "s|^GROQ_API_KEY=.*|GROQ_API_KEY=$GROQ_KEY_INPUT|" "$INSTALL_DIR/secrets.env"
        print_ok "Ключ AI (Groq) сохранён в secrets.env"
    else
        print_warn "Ключ AI пропущен — настройте позже в боте (Интеграции) или в secrets.env"
    fi

    if [ -n "$TAVILY_KEY_INPUT" ]; then
        sed -i "s|^TAVILY_API_KEY=.*|TAVILY_API_KEY=$TAVILY_KEY_INPUT|" "$INSTALL_DIR/secrets.env"
        print_ok "Ключ веб-поиска (Tavily) сохранён в secrets.env"
    else
        print_warn "Ключ веб-поиска пропущен — настройте позже в боте (Интеграции) или в secrets.env"
    fi

    # Генерируем случайный SUPPORT_API_TOKEN, если он ещё не задан
    if grep -q "^SUPPORT_API_TOKEN=ваш_support_api_token" "$INSTALL_DIR/secrets.env" 2>/dev/null; then
        random_token=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || echo "$(date +%s)$(shuf -i 1000-9999 -n1)")
        sed -i "s|^SUPPORT_API_TOKEN=.*|SUPPORT_API_TOKEN=$random_token|" "$INSTALL_DIR/secrets.env"
        print_ok "SUPPORT_API_TOKEN сгенерирован автоматически"
    fi

    for provider in GOOGLE YANDEX VK; do
        id_var="${provider}_CLIENT_ID_INPUT"
        secret_var="${provider}_CLIENT_SECRET_INPUT"
        id_val="${!id_var}"
        secret_val="${!secret_var}"
        if [ -n "$id_val" ] && [ -n "$secret_val" ]; then
            sed -i "s|^${provider}_OAUTH_CLIENT_ID=.*|${provider}_OAUTH_CLIENT_ID=$id_val|" "$INSTALL_DIR/secrets.env"
            sed -i "s|^${provider}_OAUTH_CLIENT_SECRET=.*|${provider}_OAUTH_CLIENT_SECRET=$secret_val|" "$INSTALL_DIR/secrets.env"
            print_ok "$provider OAuth сохранён в secrets.env"
        fi
    done
}

# Установка системных пакетов
install_system_deps() {
    print_header "Установка системных зависимостей"

    export DEBIAN_FRONTEND=noninteractive
    export NEEDRESTART_MODE=a

    apt-get update -qq
    apt-get install -y -qq \
        python3-venv \
        python3-pip \
        git \
        > /dev/null 2>&1

    print_ok "Системные пакеты обновлены"
    print_ok "python3-venv, python3-pip, git установлены"
}

# Создание виртуального окружения и установка зависимостей
setup_venv() {
    print_header "Настройка виртуального окружения Python"

    python3 -m venv "$VENV_DIR"
    print_ok "Виртуальное окружение создано: $VENV_DIR"

    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    pip install --upgrade -r "$INSTALL_DIR/requirements.txt" -q
    deactivate

    print_ok "Зависимости Python установлены в venv"
}

# Настройка systemd сервиса
setup_systemd() {
    print_header "Настройка автозапуска (systemd)"

    cat > "$INSTALL_DIR/$SERVICE_FILE" << EOF
[Unit]
Description=ECLIPSE Unlimited VPN Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=-$INSTALL_DIR/secrets.env
ExecStart=$VENV_DIR/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    cp "$INSTALL_DIR/$SERVICE_FILE" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable eclipse-vpn > /dev/null 2>&1

    print_ok "systemd сервис установлен и включён в автозапуск"

    # AI-консультант — отдельный сервис (FastAPI на порту 8086), работает
    # только если задан GROQ_API_KEY. Файл сервиса уже есть в репозитории.
    if [ -f "$INSTALL_DIR/eclipse-ai.service" ]; then
        cp "$INSTALL_DIR/eclipse-ai.service" /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable eclipse-ai > /dev/null 2>&1
        print_ok "AI-сервис установлен и включён в автозапуск"
    fi
}

# Запуск сервиса
start_service() {
    systemctl start eclipse-vpn
    if [ -f /etc/systemd/system/eclipse-ai.service ]; then
        systemctl start eclipse-ai
    fi
    sleep 2

    if systemctl is-active --quiet eclipse-vpn; then
        print_ok "Бот запущен и работает!"
    else
        print_err "Бот не запустился. Проверьте логи:"
        echo "  systemctl status eclipse-vpn"
        echo "  journalctl -u eclipse-vpn -n 50"
    fi
}

# ============================================================
# ПУНКТ 1: УСТАНОВКА
# ============================================================
do_install() {
    print_header "🚀 Установка ECLIPSE Unlimited VPN"

    # Проверяем, не установлен ли уже
    if [ -d "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR/.git" ]; then
        print_warn "ECLIPSE Unlimited VPN уже установлен в $INSTALL_DIR"
        if [ "$AUTO_MODE" = "1" ]; then
            print_warn "Автоматический режим: принудительная переустановка"
            reinstall_choice="1"
        else
            echo ""
            echo "  1) Переустановить (удалить и установить заново)"
            echo "  2) Отмена"
            read -p "Выберите [1-2]: " reinstall_choice
        fi
        if [ "$reinstall_choice" != "1" ]; then
            echo "Установка отменена."
            return 0
        fi
        systemctl stop eclipse-vpn 2>/dev/null || true
        # Сохраняем config.py и базу данных
        if [ -f "$INSTALL_DIR/config.py" ]; then
            cp "$INSTALL_DIR/config.py" /tmp/eclipse_config_backup.py
            BACKUP_CONFIG=1
        fi
        if [ -f "$INSTALL_DIR/vpn_bot.db" ]; then
            cp "$INSTALL_DIR/vpn_bot.db" /tmp/eclipse_db_backup.db
            BACKUP_DB=1
        fi
        rm -rf "$INSTALL_DIR"
    fi

    # Запрашиваем настройки до начала установки
    ask_config

    # Установка системных зависимостей
    install_system_deps

    # Клонирование репозитория
    print_header "Загрузка ECLIPSE Unlimited VPN"
    git clone "$REPO_URL" "$INSTALL_DIR" -q
    cd "$INSTALL_DIR"
    print_ok "Репозиторий клонирован"

    # Восстановление backup'ов при переустановке
    if [ "$BACKUP_CONFIG" = "1" ] && [ -f "/tmp/eclipse_config_backup.py" ]; then
        cp /tmp/eclipse_config_backup.py "$INSTALL_DIR/config.py"
        rm /tmp/eclipse_config_backup.py
        print_ok "config.py восстановлен из резервной копии"
        NEED_WRITE_CONFIG=0
    fi
    if [ "$BACKUP_DB" = "1" ] && [ -f "/tmp/eclipse_db_backup.db" ]; then
        cp /tmp/eclipse_db_backup.db "$INSTALL_DIR/vpn_bot.db"
        rm /tmp/eclipse_db_backup.db
        print_ok "База данных восстановлена из резервной копии"
    fi

    # Запись config.py
    write_config

    # Виртуальное окружение и зависимости
    setup_venv

    # Настройка автозапуска
    setup_systemd

    # Запуск
    print_header "Запуск бота"
    start_service

    print_header "✅ Установка завершена!"
    echo -e "  Директория: ${GREEN}$INSTALL_DIR${NC}"
    echo -e "  Виртуальное окружение: ${GREEN}$VENV_DIR${NC}"
    echo -e "  Управление сервисом:"
    echo -e "    ${CYAN}systemctl status eclipse-vpn${NC}   — статус"
    echo -e "    ${CYAN}systemctl restart eclipse-vpn${NC}  — перезапуск"
    echo -e "    ${CYAN}systemctl stop eclipse-vpn${NC}     — остановка"
    echo -e "    ${CYAN}journalctl -u eclipse-vpn -f${NC}   — логи"
}

# ============================================================
# ПУНКТ 2: МЯГКОЕ ОБНОВЛЕНИЕ (git pull)
# ============================================================
do_soft_update() {
    print_header "🔄 Мягкое обновление"

    if [ ! -d "$INSTALL_DIR/.git" ]; then
        print_err "ECLIPSE Unlimited VPN не установлен в $INSTALL_DIR"
        return 1
    fi

    cd "$INSTALL_DIR"

    # Сохраняем текущие изменения в stash (если есть)
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        print_warn "Обнаружены локальные изменения — сохраняем через git stash"
        git stash -q
        STASHED=1
    fi

    if [ -n "$TARGET_COMMIT" ]; then
        git fetch -q origin
        git checkout -q "$TARGET_COMMIT"
    else
        git checkout -q main
        git pull -q origin main
    fi

    if [ "$STASHED" = "1" ]; then
        git stash pop -q 2>/dev/null || print_warn "Не удалось восстановить локальные изменения (конфликт)"
    fi

    print_ok "Код обновлён"

    # Обновляем зависимости
    source "$VENV_DIR/bin/activate"
    pip install --upgrade -r requirements.txt -q
    deactivate
    print_ok "Зависимости обновлены"

    # Перезапуск
    systemctl restart eclipse-vpn
    if [ -f /etc/systemd/system/eclipse-ai.service ]; then
        systemctl restart eclipse-ai
    fi
    sleep 2

    if systemctl is-active --quiet eclipse-vpn; then
        print_ok "Бот перезапущен и работает!"
    else
        print_err "Бот не запустился после обновления"
        echo "  systemctl status eclipse-vpn"
    fi
}

# ============================================================
# ПУНКТ 3: ЖЁСТКАЯ ПЕРЕЗАПИСЬ (git fetch + reset)
# ============================================================
do_hard_reset() {
    print_header "⚠️  Жёсткая перезапись"

    if [ ! -d "$INSTALL_DIR/.git" ]; then
        print_err "ECLIPSE Unlimited VPN не установлен в $INSTALL_DIR"
        return 1
    fi

    echo -e "${RED}Внимание! Все локальные изменения в коде будут перезаписаны.${NC}"
    echo -e "${YELLOW}config.py и vpn_bot.db затронуты НЕ будут.${NC}"
    if [ "$AUTO_MODE" = "1" ]; then
        confirm="y"
    else
        read -p "Продолжить? (y/N): " confirm
    fi
    if [[ ! "$confirm" =~ ^[YyДд]$ ]]; then
        echo "Отменено."
        return 0
    fi

    cd "$INSTALL_DIR"

    # Жёсткая перезапись: config.py и vpn_bot.db в .gitignore — не затрагиваются
    git fetch origin -q
    local target="origin/main"
    if [ -n "$TARGET_COMMIT" ]; then
        target="$TARGET_COMMIT"
    fi
    git reset --hard "$target" -q
    git clean -fd -q
    print_ok "Код перезаписан ($target)"

    # Обновляем зависимости
    source "$VENV_DIR/bin/activate"
    pip install --upgrade -r requirements.txt -q
    deactivate
    print_ok "Зависимости обновлены"

    # Перезапуск
    systemctl restart eclipse-vpn
    if [ -f /etc/systemd/system/eclipse-ai.service ]; then
        systemctl restart eclipse-ai
    fi
    sleep 2

    if systemctl is-active --quiet eclipse-vpn; then
        print_ok "Бот перезапущен и работает!"
    else
        print_err "Бот не запустился после перезаписи"
        echo "  systemctl status eclipse-vpn"
    fi
}

# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================
show_menu() {
    clear
    echo -e "${CYAN}"
    echo "  ╔═══════════════════════════════════════╗"
    echo "  ║       🌐 ECLIPSE Unlimited VPN Manager         ║"
    echo "  ╚═══════════════════════════════════════╝"
    echo -e "${NC}"
    echo "  1) 🚀 Установка"
    echo "  2) 🔄 Мягкое обновление (git pull)"
    echo "  3) ⚠️  Жёсткая перезапись (с GitHub)"
    echo ""
    echo "  0) Выход"
    echo ""
    read -p "  Выберите действие [0-3]: " choice

    case $choice in
        1) do_install ;;
        2) do_soft_update ;;
        3) do_hard_reset ;;
        0) echo "Пока! 👋"; exit 0 ;;
        *) echo "Неверный выбор"; return 1 ;;
    esac
}

# Проверка root-прав
if [ "$EUID" -ne 0 ]; then
    print_err "Скрипт должен быть запущен от root (sudo)"
    exit 1
fi

# Проверка на автоматический режим (передан аргумент действия)
if [ -n "$1" ]; then
    ACTION="$1"
    export AUTO_MODE="1"
    
    case "$ACTION" in
        install)
            if [ -z "$2" ] || [ -z "$3" ]; then
                print_err "Для автоматической установки требуются BOT_TOKEN и ADMIN_ID"
                echo "Использование: bash install.sh install <BOT_TOKEN> <ADMIN_ID>"
                exit 1
            fi
            export BOT_TOKEN="$2"
            export ADMIN_ID="$3"
            do_install 
            ;;
        update)
            export TARGET_COMMIT="$2"
            do_soft_update 
            ;;
        reset)
            export TARGET_COMMIT="$2"
            do_hard_reset 
            ;;
        *)
            print_err "Неизвестное действие: $ACTION. Доступно: install, update, reset"
            exit 1
            ;;
    esac
    exit 0
fi

show_menu
