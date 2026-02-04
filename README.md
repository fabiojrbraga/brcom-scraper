# Instagram Scraper API

Sistema de raspagem de dados do Instagram usando IA Generativa, Browser Automation e Browserless.

## 📋 Características

- **Browser Automation**: Usa Browserless para automação de navegador headless
- **IA Generativa**: Integração com OpenAI para extração inteligente de dados
- **Análise Híbrida**: Combina visão computacional com processamento de texto
- **API REST**: FastAPI com endpoints bem documentados
- **PostgreSQL**: Persistência de dados estruturados
- **Docker**: Pronto para deploy em EasyPanel e outros ambientes containerizados

## 🏗️ Arquitetura

```
┌─────────────────────────────────────────────────────┐
│         FastAPI Backend (Python)                    │
├─────────────────────────────────────────────────────┤
│  POST /api/scrape - Inicia scraping                 │
│  GET  /api/scrape/{job_id} - Status do job          │
│  GET  /api/scrape/{job_id}/results - Resultados     │
│  GET  /api/profiles/{username} - Info do perfil     │
│  GET  /api/profiles/{username}/posts - Posts        │
│  GET  /api/profiles/{username}/interactions - Ints  │
├─────────────────────────────────────────────────────┤
│  Browserless (Headless Browser)                     │
│  ├─ Screenshots                                     │
│  ├─ HTML Extraction                                │
│  └─ JavaScript Execution                           │
├─────────────────────────────────────────────────────┤
│  OpenAI API (IA Generativa)                         │
│  ├─ Vision (Análise de imagens)                     │
│  └─ GPT-4 Mini (Processamento de texto)             │
├─────────────────────────────────────────────────────┤
│  PostgreSQL (Persistência)                          │
│  ├─ Profiles                                        │
│  ├─ Posts                                           │
│  ├─ Interactions                                    │
│  └─ Scraping Jobs                                   │
└─────────────────────────────────────────────────────┘
```

## 🚀 Instalação

### Pré-requisitos

- Docker e Docker Compose
- Python 3.11+ (para desenvolvimento local)
- Conta Browserless com token
- API Key OpenAI

### Variáveis de Ambiente

Copie `.env.example` para `.env` e configure:

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
DATABASE_URL=postgresql://user:password@host:5432/instagram_scraper

# Browserless
BROWSERLESS_HOST=https://your-browserless-instance.com
BROWSERLESS_TOKEN=your-browserless-token

# OpenAI
OPENAI_API_KEY=sk-your-api-key-here
```

### Login humano + import de sessao (sem senha no Browser Use)

Se voce nao quiser expor usuario/senha do Instagram para a IA, use o fluxo manual:

```bash
# 1) Capturar storage_state apos login humano (abre navegador)
python scripts/capture_instagram_session.py --mode local

# 2) Importar sessao no banco
python scripts/import_instagram_session.py --username seu_usuario
```

Arquivo gerado: `.secrets/instagram_storage_state.json` (ignorado no git).

Para escolher uma sessao especifica na API, envie `session_username` no body:

```json
{
  "profile_url": "https://www.instagram.com/username/",
  "session_username": "conta_logada"
}
```

Endpoints administrativos de sessao:

```bash
# listar sessoes (ativas por padrao)
GET /api/instagram_sessions?active_only=true&username=conta_logada

# desativar sessao especifica
POST /api/instagram_sessions/{session_id}/deactivate
```

Observacao: o script de captura salva tambem o `user_agent` da sessao e o scraper tenta reutilizar esse valor nos requests Browserless (`userAgent` no payload, com fallback automatico quando nao suportado).

### Desenvolvimento Local

```bash
# Instalar dependências
pip install -r requirements.txt

# Iniciar com Docker Compose
docker-compose up -d

# Acessar API
# http://localhost:8000
# Documentação: http://localhost:8000/docs
```

### Deploy em EasyPanel

1. **Criar novo aplicativo**:
   - Tipo: Docker
   - Dockerfile: Use o fornecido
   - Porta: 8000

2. **Configurar variáveis de ambiente** no EasyPanel:
   ```
   DATABASE_URL=postgresql://...
   BROWSERLESS_HOST=...
   BROWSERLESS_TOKEN=...
   OPENAI_API_KEY=sk-...
   ```

3. **Deploy**:
   - Push para repositório Git
   - EasyPanel fará build e deploy automaticamente

## 📚 Uso da API

### 1. Iniciar Scraping

```bash
curl -X POST http://localhost:8000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{"profile_url": "https://instagram.com/username"}'
```

Resposta:
```json
{
  "id": "job-uuid",
  "profile_url": "https://instagram.com/username",
  "status": "pending",
  "created_at": "2024-01-28T10:30:00Z"
}
```

### 2. Verificar Status

```bash
curl http://localhost:8000/api/scrape/{job_id}
```

### 3. Obter Resultados

```bash
curl http://localhost:8000/api/scrape/{job_id}/results
```

Resposta:
```json
{
  "job_id": "job-uuid",
  "status": "completed",
  "profile": {
    "username": "example_user",
    "profile_url": "https://instagram.com/example_user",
    "bio": "Bio do perfil",
    "is_private": false,
    "follower_count": 1500,
    "posts": [
      {
        "post_url": "https://instagram.com/p/ABC123",
        "caption": "Caption do post",
        "like_count": 250,
        "comment_count": 15,
        "interactions": [
          {
            "type": "comment",
            "user_url": "https://instagram.com/user1",
            "user_username": "user1",
            "user_bio": "Bio do user1",
            "is_private": false,
            "comment_text": "Que legal! 😍"
          }
        ]
      }
    ]
  },
  "total_posts": 5,
  "total_interactions": 42,
  "completed_at": "2024-01-28T10:35:00Z"
}
```

### 4. Obter Perfil

```bash
curl http://localhost:8000/api/profiles/username
```

### 5. Obter Posts do Perfil

```bash
curl http://localhost:8000/api/profiles/username/posts?skip=0&limit=10
```

### 6. Obter Interações do Perfil

```bash
curl http://localhost:8000/api/profiles/username/interactions?skip=0&limit=50
```

## 🗄️ Estrutura do Banco de Dados

### Profiles
```sql
- id (UUID)
- instagram_username (String, Unique)
- instagram_url (String)
- bio (Text)
- is_private (Boolean)
- follower_count (Integer)
- verified (Boolean)
- created_at (DateTime)
- updated_at (DateTime)
- last_scraped_at (DateTime)
```

### Posts
```sql
- id (UUID)
- profile_id (FK)
- post_url (String, Unique)
- caption (Text)
- like_count (Integer)
- comment_count (Integer)
- posted_at (DateTime)
- created_at (DateTime)
- updated_at (DateTime)
```

### Interactions
```sql
- id (UUID)
- post_id (FK)
- profile_id (FK)
- user_username (String)
- user_url (String)
- user_bio (Text)
- user_is_private (Boolean)
- interaction_type (Enum: like, comment, share, save)
- comment_text (Text)
- comment_likes (Integer)
- comment_replies (Integer)
- created_at (DateTime)
- updated_at (DateTime)
```

### Scraping Jobs
```sql
- id (UUID)
- profile_url (String)
- status (String: pending, running, completed, failed)
- started_at (DateTime)
- completed_at (DateTime)
- error_message (Text)
- posts_scraped (Integer)
- interactions_scraped (Integer)
- created_at (DateTime)
```

## 🔄 Fluxo de Scraping

1. **Requisição**: Cliente envia URL do perfil
2. **Job Creation**: Sistema cria job com status "pending"
3. **Background Task**: Scraping inicia em background
4. **Navigation**: Browserless acessa o perfil
5. **Capture**: Screenshots e HTML são capturados
6. **Extraction**: IA extrai dados estruturados
7. **Persistence**: Dados são salvos no PostgreSQL
8. **Completion**: Job atualizado com status "completed"
9. **Retrieval**: Cliente consulta resultados via API

## 📊 Dados Extraídos

### Por Perfil
- Username
- Bio
- Status privado/público
- Número de seguidores
- Verificação azul
- Data da última raspagem

### Por Post
- URL do post
- Caption/Descrição
- Número de likes
- Número de comentários
- Data do post

### Por Interação
- Tipo (like, comentário, etc)
- Username de quem interagiu
- URL do perfil do usuário
- Bio do usuário
- Status privado/público do usuário
- Texto do comentário (se aplicável)
- Likes no comentário
- Respostas ao comentário

## ⚙️ Configuração Avançada

### Limites de Taxa

Adicione delays aleatórios para simular comportamento humano:

```python
# Em instagram_scraper.py
delay = random.uniform(1, 5)  # 1-5 segundos entre ações
await asyncio.sleep(delay)
```

### Cache de Resultados

Implemente cache para reduzir custos de IA:

```python
from functools import lru_cache

@lru_cache(maxsize=100)
async def extract_profile_info(url: str):
    # Cached extraction
    pass
```

### Retry Logic

Configure retries automáticos:

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
async def scrape_profile(url: str):
    # Will retry up to 3 times
    pass
```

## 🐛 Troubleshooting

### Erro: "Browserless não acessível"
- Verifique `BROWSERLESS_HOST` e `BROWSERLESS_TOKEN`
- Teste conexão: `curl -H "Authorization: Bearer TOKEN" https://host/health`

### Erro: "OpenAI API error"
- Verifique `OPENAI_API_KEY`
- Confirme quota e saldo da conta

### Erro: "PostgreSQL connection failed"
- Verifique `DATABASE_URL`
- Confirme que PostgreSQL está rodando

### Erro: "Instagram bloqueou requisição"
- Aumente delays entre requisições
- Considere usar proxy
- Implemente retry com backoff exponencial

## 📈 Performance

### Otimizações Implementadas

1. **Multi-stage Docker Build**: Reduz tamanho da imagem
2. **Connection Pooling**: Reutiliza conexões PostgreSQL
3. **Async/Await**: Processamento não-bloqueante
4. **Background Tasks**: Scraping não bloqueia API
5. **Batch Processing**: Processa múltiplos itens por chamada IA

### Benchmarks Esperados

- Scraping de 1 perfil: 30-60 segundos
- Extração de 5 posts: 15-30 segundos
- Custo por perfil: $0.50 - $1.50 (com gpt-4-mini)

## 🔐 Segurança

- Variáveis sensíveis via `.env` (não commitadas)
- CORS configurado para produção
- Rate limiting (implementar conforme necessário)
- Validação de entrada com Pydantic
- SQL Injection protection via SQLAlchemy ORM

## 📝 Logging

Logs são configurados por nível:

```
INFO: Operações normais
WARNING: Situações anormais
ERROR: Erros que precisam atenção
DEBUG: Informações detalhadas (desenvolvimento)
```

Visualize logs:
```bash
docker logs instagram-scraper-app -f
```

## 🤝 Contribuindo

1. Fork o projeto
2. Crie uma branch para sua feature (`git checkout -b feature/AmazingFeature`)
3. Commit suas mudanças (`git commit -m 'Add some AmazingFeature'`)
4. Push para a branch (`git push origin feature/AmazingFeature`)
5. Abra um Pull Request

## 📄 Licença

Este projeto está sob a licença MIT. Veja `LICENSE` para mais detalhes.

## 📞 Suporte

Para suporte, abra uma issue no repositório ou entre em contato.

## 🙏 Agradecimentos

- OpenAI pela API GPT
- Browserless pela infraestrutura de navegador
- FastAPI pela framework web
- SQLAlchemy pelo ORM

---

**Última atualização**: 28 de Janeiro de 2024
**Versão**: 1.0.0
