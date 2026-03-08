#!/bin/bash

# Скрипт для запуска локального HTTP сервера для документации API
# VK Music Bot - OpenAPI Documentation Server

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCS_DIR="$SCRIPT_DIR/docs"
PORT=8080

# Проверяем существование папки docs
if [ ! -d "$DOCS_DIR" ]; then
    echo "❌ Ошибка: Папка docs не найдена в $SCRIPT_DIR"
    exit 1
fi

echo "🚀 Запуск сервера документации VK Music Bot API..."
echo "📁 Директория: $DOCS_DIR"
echo "🌐 URL: http://localhost:$PORT"
echo ""
echo "Нажмите Ctrl+C для остановки сервера"
echo "----------------------------------------"

# Запускаем Python HTTP сервер
cd "$DOCS_DIR"
python3 -m http.server "$PORT" --directory .
