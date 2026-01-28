# Estrutura do Projeto - Instagram Scraper

## 📁 Árvore de Diretórios

```
instagram-scraper/
├── app/                           # Pacote principal da aplicação
│   ├── __init__.py               # Inicialização do pacote
│   ├── api/                      # Módulo de API REST
│   │   ├── __init__.py
│   │   └── routes.py             # Endpoints FastAPI
│   ├── scraper/                  # Módulo de scraping
│   │   ├── __init__.py
│   │   ├── browserless_client.py # Cliente Browserless
│   │   ├── browser_use_agent.py  # Agente Browser Use
│   │   ├── ai_extractor.py       # Extrator IA Híbrido
│   │   └── instagram_scraper.py  # Scraper Principal
│   ├── models.py                 # Modelos SQLAlchemy
│   ├── schemas.py                # Schemas Pydantic
│   └── database.py               # Configuração PostgreSQL
├── config.py                     # Configuração centralizada
├── main.py                       # Aplicação FastAPI
├── requirements.txt              # Dependências Python
├── Dockerfile                    # Containerização
├── docker-compose.yml            # Orquestração local
├── .dockerignore                 # Otimização Docker
├── .gitignore                    # Configuração Git
├── .env.example                  # Template de variáveis
├── run.sh                        # Script de inicialização
├── easypanel.yml                 # Configuração EasyPanel
├── README.md                     # Documentação principal
├── SETUP.md                      # Guia de configuração
├── EXAMPLES.md                   # Exemplos de uso
└── PROJECT_STRUCTURE.md          # Este arquivo
```

## 📄 Descrição dos Arquivos

### Raiz do Projeto

| Arquivo | Descrição |
|---------|-----------|
| `main.py` | Ponto de entrada da aplicação FastAPI |
| `config.py` | Configuração centralizada (variáveis de ambiente) |
| `requirements.txt` | Dependências Python (pip) |
| `Dockerfile` | Containerização multi-stage |
| `docker-compose.yml` | Orquestração para desenvolvimento |
| `.env.example` | Template de variáveis de ambiente |
| `run.sh` | Script para iniciar em desenvolvimento |
| `easypanel.yml` | Configuração para deploy no EasyPanel |

### Documentação

| Arquivo | Conteúdo |
|---------|----------|
| `README.md` | Documentação principal do projeto |
| `SETUP.md` | Guia detalhado de instalação e configuração |
| `EXAMPLES.md` | Exemplos de uso da API (cURL, Python, JS) |
| `PROJECT_STRUCTURE.md` | Este arquivo |

### Módulo `app/`

#### `app/models.py`
Modelos SQLAlchemy para persistência de dados:
- `Profile` - Perfis do Instagram
- `Post` - Posts dos perfis
- `Interaction` - Interações (likes, comentários, etc)
- `ScrapingJob` - Rastreamento de jobs de scraping

#### `app/schemas.py`
Schemas Pydantic para validação de requisições/respostas:
- `ProfileResponse` - Resposta de perfil
- `PostResponse` - Resposta de post
- `InteractionResponse` - Resposta de interação
- `ScrapingJobCreate` - Requisição de scraping
- `ScrapingCompleteResponse` - Resultado completo

#### `app/database.py`
Configuração de banco de dados:
- `engine` - Engine SQLAlchemy
- `SessionLocal` - Factory de sessões
- `get_db()` - Dependência FastAPI
- `init_db()` - Inicialização de tabelas
- `health_check()` - Verificação de saúde

#### `app/api/routes.py`
Endpoints da API REST:
- `POST /api/scrape` - Iniciar scraping
- `GET /api/scrape/{job_id}` - Status do job
- `GET /api/scrape/{job_id}/results` - Resultados
- `GET /api/profiles/{username}` - Info do perfil
- `GET /api/profiles/{username}/posts` - Posts
- `GET /api/profiles/{username}/interactions` - Interações

#### `app/scraper/browserless_client.py`
Cliente para Browserless:
- `screenshot()` - Capturar screenshot
- `get_html()` - Obter HTML da página
- `execute_script()` - Executar JavaScript
- `pdf()` - Gerar PDF
- `health_check()` - Verificar saúde

#### `app/scraper/browser_use_agent.py`
Agente Browser Use para automação inteligente:
- `navigate_and_scrape_profile()` - Navegar e raspar perfil
- `scroll_and_load_more()` - Scroll infinito
- `click_and_wait()` - Clicar e aguardar
- `extract_visible_text()` - Extrair texto

#### `app/scraper/ai_extractor.py`
Extrator IA Híbrido:
- `extract_profile_info()` - Extrair info do perfil
- `extract_posts_info()` - Extrair info dos posts
- `extract_comments()` - Extrair comentários
- `extract_user_info()` - Extrair info do usuário

#### `app/scraper/instagram_scraper.py`
Scraper Principal:
- `scrape_profile()` - Raspar perfil completo
- `_scrape_posts()` - Raspar posts
- `_scrape_post_interactions()` - Raspar interações
- `_save_profile()` - Salvar perfil no banco
- `_save_posts_and_interactions()` - Salvar dados

## 🔄 Fluxo de Dados

```
Requisição HTTP
    ↓
FastAPI Router (routes.py)
    ↓
Background Task
    ↓
InstagramScraper.scrape_profile()
    ├─ BrowserlessClient.screenshot()
    ├─ BrowserlessClient.get_html()
    ├─ AIExtractor.extract_profile_info()
    ├─ AIExtractor.extract_posts_info()
    ├─ AIExtractor.extract_comments()
    ├─ Database.save_profile()
    ├─ Database.save_posts()
    └─ Database.save_interactions()
    ↓
Banco de Dados (PostgreSQL)
    ↓
Resposta JSON
```

## 🔌 Integrações Externas

```
┌─────────────────────────────────────────┐
│      Instagram Scraper API              │
├─────────────────────────────────────────┤
│                                         │
│  ┌──────────────────────────────────┐  │
│  │    Browserless (Headless)        │  │
│  │  - Screenshots                   │  │
│  │  - HTML Extraction               │  │
│  │  - JavaScript Execution          │  │
│  └──────────────────────────────────┘  │
│                                         │
│  ┌──────────────────────────────────┐  │
│  │    OpenAI API                    │  │
│  │  - Vision (gpt-4-vision)         │  │
│  │  - Text (gpt-4-mini)             │  │
│  └──────────────────────────────────┘  │
│                                         │
│  ┌──────────────────────────────────┐  │
│  │    PostgreSQL                    │  │
│  │  - Profiles                      │  │
│  │  - Posts                         │  │
│  │  - Interactions                  │  │
│  │  - Scraping Jobs                 │  │
│  └──────────────────────────────────┘  │
│                                         │
└─────────────────────────────────────────┘
```

## 📊 Modelos de Dados

### Profile
```python
id: UUID
instagram_username: String (Unique)
instagram_url: String
bio: Text
is_private: Boolean
follower_count: Integer
following_count: Integer
post_count: Integer
verified: Boolean
created_at: DateTime
updated_at: DateTime
last_scraped_at: DateTime
```

### Post
```python
id: UUID
profile_id: UUID (FK)
post_url: String (Unique)
caption: Text
like_count: Integer
comment_count: Integer
share_count: Integer
save_count: Integer
posted_at: DateTime
created_at: DateTime
updated_at: DateTime
```

### Interaction
```python
id: UUID
post_id: UUID (FK)
profile_id: UUID (FK)
user_username: String
user_url: String
user_bio: Text
user_is_private: Boolean
user_follower_count: Integer
interaction_type: Enum (like, comment, share, save)
comment_text: Text
comment_likes: Integer
comment_replies: Integer
created_at: DateTime
updated_at: DateTime
```

### ScrapingJob
```python
id: UUID
profile_url: String
status: String (pending, running, completed, failed)
started_at: DateTime
completed_at: DateTime
error_message: Text
posts_scraped: Integer
interactions_scraped: Integer
created_at: DateTime
```

## 🔐 Variáveis de Ambiente

```env
# FastAPI
FASTAPI_ENV=production|development
FASTAPI_HOST=0.0.0.0
FASTAPI_PORT=8000

# PostgreSQL
DATABASE_URL=postgresql://user:password@host:port/database

# Browserless
BROWSERLESS_HOST=https://...
BROWSERLESS_TOKEN=...

# OpenAI
OPENAI_API_KEY=sk-...

# Instagram (opcional)
INSTAGRAM_USERNAME=...
INSTAGRAM_PASSWORD=...

# Application
LOG_LEVEL=INFO|DEBUG|WARNING|ERROR
MAX_RETRIES=3
REQUEST_TIMEOUT=30
```

## 🚀 Endpoints da API

### Scraping
- `POST /api/scrape` - Iniciar scraping
- `GET /api/scrape/{job_id}` - Status do job
- `GET /api/scrape/{job_id}/results` - Resultados

### Perfis
- `GET /api/profiles/{username}` - Info do perfil
- `GET /api/profiles/{username}/posts` - Posts
- `GET /api/profiles/{username}/interactions` - Interações

### Saúde
- `GET /api/health` - Health check
- `GET /` - Info da API

## 📦 Dependências Principais

| Pacote | Versão | Uso |
|--------|--------|-----|
| fastapi | 0.104.1 | Framework web |
| uvicorn | 0.24.0 | Servidor ASGI |
| sqlalchemy | 2.0.23 | ORM |
| psycopg2 | 2.9.9 | Driver PostgreSQL |
| openai | 1.3.8 | API OpenAI |
| playwright | 1.40.0 | Browser automation |
| browser-use | 0.1.0 | Agente IA |
| pydantic | 2.5.0 | Validação |
| python-dotenv | 1.0.0 | Variáveis .env |

## 🐳 Docker

### Dockerfile
- Multi-stage build para otimizar tamanho
- Python 3.11-slim como base
- Health check configurado
- Porta 8000 exposta

### docker-compose.yml
- Serviço `app` (FastAPI)
- Serviço `postgres` (PostgreSQL)
- Volume para dados do banco
- Network compartilhada

## 📝 Logging

Logs são estruturados com:
- Timestamp
- Nome do módulo
- Nível (INFO, WARNING, ERROR, DEBUG)
- Mensagem

Exemplo:
```
2024-01-28 10:30:00,123 - app.scraper.instagram_scraper - INFO - 🚀 Iniciando scraping do perfil: https://instagram.com/username
```

## 🔄 Ciclo de Vida da Aplicação

1. **Startup**
   - Carregar configurações
   - Inicializar banco de dados
   - Criar tabelas (se não existirem)
   - Verificar saúde das dependências

2. **Runtime**
   - Aceitar requisições HTTP
   - Executar jobs em background
   - Persistir dados

3. **Shutdown**
   - Fechar conexões
   - Limpar recursos
   - Salvar estado

## 🧪 Testes

Estrutura recomendada para testes:

```
tests/
├── __init__.py
├── conftest.py              # Fixtures pytest
├── test_api.py              # Testes de API
├── test_scraper.py          # Testes de scraper
├── test_database.py         # Testes de banco
└── test_integration.py      # Testes de integração
```

## 📈 Performance

### Otimizações Implementadas

1. **Multi-stage Docker Build** - Reduz tamanho da imagem
2. **Connection Pooling** - Reutiliza conexões DB
3. **Async/Await** - Processamento não-bloqueante
4. **Background Tasks** - Scraping não bloqueia API
5. **Batch Processing** - Múltiplos itens por chamada IA

### Benchmarks Esperados

- Scraping de 1 perfil: 30-60 segundos
- Extração de 5 posts: 15-30 segundos
- Custo por perfil: $0.50 - $1.50

## 🔐 Segurança

- Variáveis sensíveis via `.env`
- CORS configurado
- SQL Injection protection (SQLAlchemy ORM)
- Validação de entrada (Pydantic)
- Rate limiting (implementável)

---

**Última atualização**: 28 de Janeiro de 2024
**Versão**: 1.0.0
