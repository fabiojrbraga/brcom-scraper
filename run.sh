#!/bin/bash

# Script para iniciar a aplicação em desenvolvimento

set -e

echo "🚀 Instagram Scraper - Script de Inicialização"
echo "=============================================="

# Verificar se .env existe
if [ ! -f .env ]; then
    echo "⚠️  Arquivo .env não encontrado!"
    echo "📋 Criando .env a partir de .env.example..."
    cp .env.example .env
    echo "✅ Arquivo .env criado. Configure as variáveis de ambiente!"
    exit 1
fi

# Verificar Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 não encontrado!"
    exit 1
fi

echo "✅ Python encontrado: $(python3 --version)"

# Criar venv se não existir
if [ ! -d "venv" ]; then
    echo "📦 Criando ambiente virtual..."
    python3 -m venv venv
fi

# Ativar venv
echo "🔌 Ativando ambiente virtual..."
source venv/bin/activate

# Instalar dependências
echo "📥 Instalando dependências..."
pip install -q -r requirements.txt

# Inicializar banco de dados (opcional)
if [ "$1" == "--init-db" ]; then
    echo "🗄️  Inicializando banco de dados..."
    python3 -c "from app.database import init_db; init_db()"
fi

# Iniciar aplicação
echo ""
echo "🎯 Iniciando aplicação..."
echo "📍 API disponível em: http://localhost:8000"
echo "📚 Documentação em: http://localhost:8000/docs"
echo ""

python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
