# Guia de Configuração - Instagram Scraper

Este documento contém instruções detalhadas para configurar e executar o Instagram Scraper.

## 📋 Pré-requisitos

### Requisitos Mínimos
- Docker 20.10+
- Docker Compose 1.29+
- Python 3.11+ (para desenvolvimento local)
- 2GB RAM mínimo
- Conexão com Internet

### Contas Necessárias
1. **Browserless**: https://www.browserless.io
   - Token de autenticação
   - Host da instância

2. **OpenAI**: https://platform.openai.com
   - API Key
   - Saldo na conta

3. **PostgreSQL**: Banco de dados (local ou remoto)

---

## 🚀 Instalação Rápida (Docker)

### 1. Clonar Repositório

```bash
git clone https://github.com/fabiojrbraga/instagram-scraper.git
cd instagram-scraper
```

### 2. Configurar Variáveis de Ambiente

```bash
cp .env.example .env
```

Edite `.env` com suas credenciais:

```env
# FastAPI
FASTAPI_ENV=production
FASTAPI_HOST=0.0.0.0
FASTAPI_PORT=8000

# PostgreSQL
DATABASE_URL=postgresql://user:password@postgres:5432/instagram_scraper

# Browserless
BROWSERLESS_HOST=https://chrome.browserless.io
BROWSERLESS_TOKEN=seu-token-aqui

# OpenAI
OPENAI_API_KEY=sk-sua-chave-aqui

# Instagram (opcional)
INSTAGRAM_USERNAME=seu_usuario
INSTAGRAM_PASSWORD=sua_senha
```

### 3. Iniciar com Docker Compose

```bash
docker-compose up -d
```

### 4. Verificar Status

```bash
# Ver logs
docker-compose logs -f app

# Verificar saúde
curl http://localhost:8000/api/health

# Acessar documentação
# http://localhost:8000/docs
```

---

## 💻 Instalação para Desenvolvimento

### 1. Criar Ambiente Virtual

```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# ou
venv\Scripts\activate  # Windows
```

### 2. Instalar Dependências

```bash
pip install -r requirements.txt
```

### 3. Configurar Banco de Dados

#### Opção A: PostgreSQL Local

```bash
# Instalar PostgreSQL
# Ubuntu/Debian
sudo apt-get install postgresql postgresql-contrib

# macOS
brew install postgresql

# Iniciar serviço
sudo systemctl start postgresql  # Linux
brew services start postgresql  # macOS

# Criar banco de dados
psql -U postgres -c "CREATE DATABASE instagram_scraper;"
psql -U postgres -c "CREATE USER scraper WITH PASSWORD 'scraper_password';"
psql -U postgres -c "ALTER ROLE scraper SET client_encoding TO 'utf8';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE instagram_scraper TO scraper;"
```

#### Opção B: PostgreSQL via Docker

```bash
docker run -d \
  --name instagram-scraper-db \
  -e POSTGRES_USER=scraper \
  -e POSTGRES_PASSWORD=scraper_password \
  -e POSTGRES_DB=instagram_scraper \
  -p 5432:5432 \
  postgres:15-alpine
```

### 4. Configurar Variáveis de Ambiente

```bash
cp .env.example .env
```

Para desenvolvimento local:

```env
FASTAPI_ENV=development
DATABASE_URL=postgresql://scraper:scraper_password@localhost:5432/instagram_scraper
BROWSERLESS_HOST=https://chrome.browserless.io
BROWSERLESS_TOKEN=seu-token
OPENAI_API_KEY=sk-sua-chave
```

### 5. Inicializar Banco de Dados

```bash
python3 -c "from app.database import init_db; init_db()"
```

### 6. Executar Aplicação

```bash
# Opção 1: Script
./run.sh

# Opção 2: Direto
python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

## 🐳 Deploy no EasyPanel

### 1. Preparar Repositório

```bash
git init
git add .
git commit -m "Initial commit"
git push origin main
```

### 2. Conectar ao EasyPanel

1. Acesse https://easypanel.io
2. Crie uma conta ou faça login
3. Clique em "New App"
4. Selecione "Docker"
5. Conecte seu repositório Git

### 3. Configurar Variáveis de Ambiente

No painel do EasyPanel:

1. Vá para "Environment Variables"
2. Adicione as seguintes variáveis:

```
FASTAPI_ENV=production
FASTAPI_HOST=0.0.0.0
FASTAPI_PORT=8000
DATABASE_URL=postgresql://...
BROWSERLESS_HOST=https://...
BROWSERLESS_TOKEN=...
OPENAI_API_KEY=sk-...
LOG_LEVEL=INFO
```

### 4. Configurar Banco de Dados

1. No EasyPanel, crie um serviço PostgreSQL
2. Copie a CONNECTION STRING
3. Cole em `DATABASE_URL`

### 5. Deploy

1. Clique em "Deploy"
2. Aguarde o build e deploy
3. Acesse a URL fornecida

---

## 🔧 Configuração Avançada

### Aumentar Limites de Timeout

Se os jobs estão expirando:

```env
REQUEST_TIMEOUT=60  # Aumentar de 30 para 60 segundos
```

### Configurar Logging

```env
LOG_LEVEL=DEBUG  # Para mais detalhes
```

### Limitar Retries

```env
MAX_RETRIES=5  # Aumentar tentativas
```

### Usar Proxy (opcional)

Se o Instagram bloquear requisições:

```python
# Em app/scraper/browserless_client.py
proxy = {
    "http": "http://proxy-host:port",
    "https": "http://proxy-host:port"
}
```

---

## 🧪 Testes

### Teste de Conexão

```bash
# Verificar API
curl http://localhost:8000/api/health

# Verificar Banco de Dados
psql -U scraper -d instagram_scraper -c "SELECT 1;"

# Verificar Browserless
curl -H "Authorization: Bearer TOKEN" https://chrome.browserless.io/health

# Verificar OpenAI
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer sk-..."
```

### Teste de Scraping

```bash
# Iniciar scraping
curl -X POST http://localhost:8000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{"profile_url": "https://instagram.com/instagram"}'

# Verificar status
curl http://localhost:8000/api/scrape/{job_id}

# Obter resultados
curl http://localhost:8000/api/scrape/{job_id}/results
```

---

## 🐛 Troubleshooting

### Erro: "PostgreSQL connection failed"

**Causa**: Banco de dados não está acessível

**Solução**:
```bash
# Verificar se PostgreSQL está rodando
sudo systemctl status postgresql

# Verificar conexão
psql -U scraper -h localhost -d instagram_scraper -c "SELECT 1;"

# Verificar DATABASE_URL no .env
echo $DATABASE_URL
```

### Erro: "Browserless not accessible"

**Causa**: Token inválido ou host incorreto

**Solução**:
```bash
# Verificar token
echo $BROWSERLESS_TOKEN

# Testar conexão
curl -H "Authorization: Bearer $BROWSERLESS_TOKEN" \
  $BROWSERLESS_HOST/health

# Verificar se host está correto
# Deve ser: https://chrome.browserless.io ou sua instância
```

### Erro: "OpenAI API error"

**Causa**: API Key inválida ou quota excedida

**Solução**:
```bash
# Verificar API Key
echo $OPENAI_API_KEY

# Testar chamada
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"

# Verificar saldo em: https://platform.openai.com/account/billing/overview
```

### Erro: "Instagram bloqueou requisição"

**Causa**: Muitas requisições em pouco tempo

**Solução**:
1. Aumentar delays entre requisições
2. Usar proxy
3. Implementar rate limiting
4. Usar conta diferente

```python
# Aumentar delays
delay = random.uniform(5, 15)  # 5-15 segundos
await asyncio.sleep(delay)
```

### Erro: "Docker image build failed"

**Causa**: Erro durante build da imagem

**Solução**:
```bash
# Limpar cache Docker
docker system prune -a

# Rebuildar
docker-compose build --no-cache

# Ver logs detalhados
docker-compose build --no-cache app 2>&1 | tail -50
```

### Erro: "Port already in use"

**Causa**: Porta 8000 ou 5432 já está em uso

**Solução**:
```bash
# Encontrar processo usando porta
lsof -i :8000
lsof -i :5432

# Matar processo
kill -9 <PID>

# Ou usar porta diferente
FASTAPI_PORT=8001 docker-compose up
```

### Erro: "Out of memory"

**Causa**: Aplicação consumindo muita memória

**Solução**:
```bash
# Aumentar limite de memória no docker-compose.yml
services:
  app:
    mem_limit: 1g
    memswap_limit: 1g
```

---

## 📊 Monitoramento

### Ver Logs

```bash
# Logs da aplicação
docker-compose logs -f app

# Logs do banco de dados
docker-compose logs -f postgres

# Últimas 100 linhas
docker-compose logs --tail=100 app
```

### Verificar Saúde

```bash
# Health check
curl http://localhost:8000/api/health

# Documentação interativa
# Acesse: http://localhost:8000/docs
```

### Monitorar Banco de Dados

```bash
# Conectar ao banco
psql -U scraper -d instagram_scraper

# Ver tabelas
\dt

# Ver número de registros
SELECT COUNT(*) FROM profiles;
SELECT COUNT(*) FROM posts;
SELECT COUNT(*) FROM interactions;

# Ver jobs recentes
SELECT id, status, created_at FROM scraping_jobs ORDER BY created_at DESC LIMIT 10;
```

---

## 🔐 Segurança

### Boas Práticas

1. **Nunca commitar .env**
   ```bash
   # Já está em .gitignore
   ```

2. **Usar variáveis de ambiente em produção**
   ```bash
   # Não hardcode credenciais
   ```

3. **Rotacionar API Keys regularmente**
   ```bash
   # Gerar nova chave OpenAI a cada 90 dias
   ```

4. **Usar HTTPS em produção**
   ```bash
   # EasyPanel fornece SSL automático
   ```

5. **Limitar acesso ao banco de dados**
   ```bash
   # Usar firewall rules
   # Não expor porta 5432 publicamente
   ```

---

## 📈 Performance

### Otimizações

1. **Aumentar pool de conexões PostgreSQL**
   ```env
   DATABASE_URL=postgresql://user:pass@host/db?pool_size=20
   ```

2. **Cache de resultados**
   ```python
   from functools import lru_cache
   
   @lru_cache(maxsize=100)
   async def extract_profile(url):
       pass
   ```

3. **Batch processing**
   ```python
   # Processar múltiplos posts em uma chamada IA
   ```

4. **Usar modelo mais barato**
   ```python
   model = "gpt-4-mini"  # Em vez de gpt-4
   ```

---

## 📚 Recursos Adicionais

- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org)
- [OpenAI API Documentation](https://platform.openai.com/docs)
- [Browserless Documentation](https://docs.browserless.io)
- [PostgreSQL Documentation](https://www.postgresql.org/docs)
- [Docker Documentation](https://docs.docker.com)

---

## 💬 Suporte

Se encontrar problemas:

1. Verifique os logs: `docker-compose logs -f`
2. Teste conexões: `curl` para cada serviço
3. Verifique variáveis de ambiente: `echo $VAR_NAME`
4. Consulte a seção Troubleshooting acima
5. Abra uma issue no repositório

---

**Última atualização**: 28 de Janeiro de 2024
