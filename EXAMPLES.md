# Exemplos de Uso - Instagram Scraper API

Este documento contém exemplos práticos de como usar a API do Instagram Scraper.

## 📚 Índice

1. [Iniciar Scraping](#iniciar-scraping)
2. [Verificar Status](#verificar-status)
3. [Obter Resultados](#obter-resultados)
4. [Consultar Perfis](#consultar-perfis)
5. [Consultar Posts](#consultar-posts)
6. [Consultar Interações](#consultar-interações)
7. [Exemplos em Python](#exemplos-em-python)
8. [Exemplos em JavaScript](#exemplos-em-javascript)

---

## Iniciar Scraping

### cURL

```bash
curl -X POST http://localhost:8000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "profile_url": "https://instagram.com/username"
  }'
```

### Resposta

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "profile_url": "https://instagram.com/username",
  "status": "pending",
  "started_at": null,
  "completed_at": null,
  "error_message": null,
  "posts_scraped": 0,
  "interactions_scraped": 0,
  "created_at": "2024-01-28T10:30:00Z"
}
```

---

## Verificar Status

### cURL

```bash
curl http://localhost:8000/api/scrape/550e8400-e29b-41d4-a716-446655440000
```

### Resposta

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "profile_url": "https://instagram.com/username",
  "status": "running",
  "started_at": "2024-01-28T10:30:05Z",
  "completed_at": null,
  "error_message": null,
  "posts_scraped": 3,
  "interactions_scraped": 25,
  "created_at": "2024-01-28T10:30:00Z"
}
```

### Possíveis Status

- `pending` - Job aguardando execução
- `running` - Job em execução
- `completed` - Job concluído com sucesso
- `failed` - Job falhou

---

## Obter Resultados

### cURL

```bash
curl http://localhost:8000/api/scrape/550e8400-e29b-41d4-a716-446655440000/results
```

### Resposta

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "profile": {
    "username": "example_user",
    "profile_url": "https://instagram.com/example_user",
    "bio": "Fotógrafo | Viajante | Amante de café ☕",
    "is_private": false,
    "follower_count": 2500,
    "posts": [
      {
        "post_url": "https://instagram.com/p/ABC123DEF456/",
        "caption": "Pôr do sol em Barcelona 🌅",
        "like_count": 342,
        "comment_count": 28,
        "interactions": [
          {
            "type": "comment",
            "user_url": "https://instagram.com/user1",
            "user_username": "user1",
            "user_bio": "Designer gráfico",
            "is_private": false,
            "comment_text": "Que foto incrível! 🔥"
          },
          {
            "type": "comment",
            "user_url": "https://instagram.com/user2",
            "user_username": "user2",
            "user_bio": "Viajante",
            "is_private": true,
            "comment_text": "Quero ir lá!"
          },
          {
            "type": "like",
            "user_url": "https://instagram.com/user3",
            "user_username": "user3",
            "user_bio": null,
            "is_private": false,
            "comment_text": null
          }
        ]
      },
      {
        "post_url": "https://instagram.com/p/XYZ789UVW012/",
        "caption": "Café da manhã em Paris ☕🥐",
        "like_count": 521,
        "comment_count": 45,
        "interactions": [
          {
            "type": "comment",
            "user_url": "https://instagram.com/user4",
            "user_username": "user4",
            "user_bio": "Chef pasteleiro",
            "is_private": false,
            "comment_text": "Qual padaria? Preciso visitar!"
          }
        ]
      }
    ]
  },
  "total_posts": 5,
  "total_interactions": 42,
  "error_message": null,
  "completed_at": "2024-01-28T10:35:00Z"
}
```

---

## Consultar Perfis

### Obter Informações do Perfil

```bash
curl http://localhost:8000/api/profiles/example_user
```

### Resposta

```json
{
  "id": "profile-uuid-123",
  "instagram_username": "example_user",
  "instagram_url": "https://instagram.com/example_user",
  "bio": "Fotógrafo | Viajante | Amante de café ☕",
  "is_private": false,
  "follower_count": 2500,
  "following_count": 350,
  "post_count": 125,
  "verified": false,
  "created_at": "2024-01-28T10:30:00Z",
  "updated_at": "2024-01-28T10:35:00Z",
  "last_scraped_at": "2024-01-28T10:35:00Z"
}
```

---

## Consultar Posts

### Listar Posts do Perfil

```bash
curl "http://localhost:8000/api/profiles/example_user/posts?skip=0&limit=10"
```

### Resposta

```json
{
  "username": "example_user",
  "total": 5,
  "posts": [
    {
      "id": "post-uuid-1",
      "profile_id": "profile-uuid-123",
      "post_url": "https://instagram.com/p/ABC123DEF456/",
      "caption": "Pôr do sol em Barcelona 🌅",
      "like_count": 342,
      "comment_count": 28,
      "share_count": 5,
      "save_count": 12,
      "posted_at": "2024-01-25T15:30:00Z",
      "created_at": "2024-01-28T10:30:00Z",
      "updated_at": "2024-01-28T10:35:00Z"
    },
    {
      "id": "post-uuid-2",
      "profile_id": "profile-uuid-123",
      "post_url": "https://instagram.com/p/XYZ789UVW012/",
      "caption": "Café da manhã em Paris ☕🥐",
      "like_count": 521,
      "comment_count": 45,
      "share_count": 8,
      "save_count": 23,
      "posted_at": "2024-01-24T08:15:00Z",
      "created_at": "2024-01-28T10:30:00Z",
      "updated_at": "2024-01-28T10:35:00Z"
    }
  ]
}
```

### Parâmetros de Paginação

- `skip` (padrão: 0) - Número de posts a pular
- `limit` (padrão: 10, máximo: 100) - Número máximo de posts a retornar

---

## Consultar Interações

### Listar Interações do Perfil

```bash
curl "http://localhost:8000/api/profiles/example_user/interactions?skip=0&limit=50"
```

### Resposta

```json
{
  "username": "example_user",
  "total": 42,
  "interactions": [
    {
      "id": "interaction-uuid-1",
      "post_id": "post-uuid-1",
      "profile_id": "profile-uuid-123",
      "user_username": "user1",
      "user_url": "https://instagram.com/user1",
      "user_bio": "Designer gráfico",
      "user_is_private": false,
      "interaction_type": "comment",
      "comment_text": "Que foto incrível! 🔥",
      "comment_likes": 5,
      "comment_replies": 2,
      "created_at": "2024-01-25T16:45:00Z",
      "updated_at": "2024-01-28T10:35:00Z"
    },
    {
      "id": "interaction-uuid-2",
      "post_id": "post-uuid-1",
      "profile_id": "profile-uuid-123",
      "user_username": "user2",
      "user_url": "https://instagram.com/user2",
      "user_bio": "Viajante",
      "user_is_private": true,
      "interaction_type": "comment",
      "comment_text": "Quero ir lá!",
      "comment_likes": 2,
      "comment_replies": 0,
      "created_at": "2024-01-25T17:20:00Z",
      "updated_at": "2024-01-28T10:35:00Z"
    },
    {
      "id": "interaction-uuid-3",
      "post_id": "post-uuid-2",
      "profile_id": "profile-uuid-123",
      "user_username": "user4",
      "user_url": "https://instagram.com/user4",
      "user_bio": "Chef pasteleiro",
      "user_is_private": false,
      "interaction_type": "comment",
      "comment_text": "Qual padaria? Preciso visitar!",
      "comment_likes": 8,
      "comment_replies": 1,
      "created_at": "2024-01-24T09:30:00Z",
      "updated_at": "2024-01-28T10:35:00Z"
    }
  ]
}
```

### Filtrar por Tipo de Interação

Tipos disponíveis:
- `like` - Likes no post
- `comment` - Comentários
- `share` - Compartilhamentos
- `save` - Salvamentos

---

## Exemplos em Python

### Instalação

```bash
pip install requests
```

### Iniciar Scraping

```python
import requests
import time

BASE_URL = "http://localhost:8000/api"

# Iniciar scraping
response = requests.post(
    f"{BASE_URL}/scrape",
    json={"profile_url": "https://instagram.com/example_user"}
)

job_data = response.json()
job_id = job_data["id"]

print(f"Job iniciado: {job_id}")
print(f"Status: {job_data['status']}")
```

### Aguardar Conclusão

```python
def wait_for_completion(job_id, max_wait=300):
    """Aguarda até que o job seja concluído."""
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        response = requests.get(f"{BASE_URL}/scrape/{job_id}")
        job_data = response.json()
        
        print(f"Status: {job_data['status']}")
        print(f"Posts: {job_data['posts_scraped']}")
        print(f"Interações: {job_data['interactions_scraped']}")
        
        if job_data['status'] == 'completed':
            return job_data
        elif job_data['status'] == 'failed':
            raise Exception(f"Job falhou: {job_data['error_message']}")
        
        time.sleep(5)  # Verificar a cada 5 segundos
    
    raise TimeoutError("Job demorou muito tempo")

# Aguardar conclusão
job_data = wait_for_completion(job_id)
```

### Obter Resultados

```python
response = requests.get(f"{BASE_URL}/scrape/{job_id}/results")
results = response.json()

profile = results['profile']
print(f"Perfil: {profile['username']}")
print(f"Bio: {profile['bio']}")
print(f"Seguidores: {profile['follower_count']}")
print(f"Posts: {results['total_posts']}")
print(f"Interações: {results['total_interactions']}")

# Processar posts
for post in profile['posts']:
    print(f"\nPost: {post['post_url']}")
    print(f"Caption: {post['caption']}")
    print(f"Likes: {post['like_count']}")
    print(f"Comentários: {post['comment_count']}")
    
    # Processar interações
    for interaction in post['interactions']:
        print(f"  - {interaction['user_username']}: {interaction['comment_text']}")
```

### Script Completo

```python
import requests
import time
import json

BASE_URL = "http://localhost:8000/api"

def scrape_instagram_profile(username):
    """Script completo para raspar um perfil."""
    
    # 1. Iniciar scraping
    print(f"🚀 Iniciando scraping de {username}...")
    response = requests.post(
        f"{BASE_URL}/scrape",
        json={"profile_url": f"https://instagram.com/{username}"}
    )
    
    if response.status_code != 200:
        print(f"❌ Erro: {response.text}")
        return
    
    job_id = response.json()["id"]
    print(f"✅ Job criado: {job_id}")
    
    # 2. Aguardar conclusão
    print("⏳ Aguardando conclusão...")
    while True:
        response = requests.get(f"{BASE_URL}/scrape/{job_id}")
        job_data = response.json()
        
        if job_data['status'] == 'completed':
            print("✅ Scraping concluído!")
            break
        elif job_data['status'] == 'failed':
            print(f"❌ Erro: {job_data['error_message']}")
            return
        
        print(f"  Status: {job_data['status']} | Posts: {job_data['posts_scraped']} | Interações: {job_data['interactions_scraped']}")
        time.sleep(5)
    
    # 3. Obter resultados
    response = requests.get(f"{BASE_URL}/scrape/{job_id}/results")
    results = response.json()
    
    # 4. Exibir resultados
    print("\n" + "="*50)
    print(f"PERFIL: {results['profile']['username']}")
    print("="*50)
    print(f"Bio: {results['profile']['bio']}")
    print(f"Privado: {results['profile']['is_private']}")
    print(f"Seguidores: {results['profile']['follower_count']}")
    print(f"\nTotal de Posts: {results['total_posts']}")
    print(f"Total de Interações: {results['total_interactions']}")
    
    # 5. Salvar em JSON
    with open(f"{username}_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ Resultados salvos em {username}_results.json")

# Executar
if __name__ == "__main__":
    scrape_instagram_profile("example_user")
```

---

## Exemplos em JavaScript

### Instalação

```bash
npm install axios
```

### Iniciar Scraping

```javascript
const axios = require('axios');

const BASE_URL = 'http://localhost:8000/api';

async function startScraping(profileUrl) {
  try {
    const response = await axios.post(`${BASE_URL}/scrape`, {
      profile_url: profileUrl
    });
    
    console.log('Job iniciado:', response.data.id);
    return response.data.id;
  } catch (error) {
    console.error('Erro:', error.response.data);
  }
}

// Usar
startScraping('https://instagram.com/example_user');
```

### Aguardar Conclusão

```javascript
async function waitForCompletion(jobId, maxWait = 300000) {
  const startTime = Date.now();
  
  while (Date.now() - startTime < maxWait) {
    try {
      const response = await axios.get(`${BASE_URL}/scrape/${jobId}`);
      const jobData = response.data;
      
      console.log(`Status: ${jobData.status}`);
      console.log(`Posts: ${jobData.posts_scraped}`);
      console.log(`Interações: ${jobData.interactions_scraped}`);
      
      if (jobData.status === 'completed') {
        return jobData;
      } else if (jobData.status === 'failed') {
        throw new Error(`Job falhou: ${jobData.error_message}`);
      }
      
      await new Promise(resolve => setTimeout(resolve, 5000));
    } catch (error) {
      console.error('Erro:', error.message);
    }
  }
  
  throw new Error('Job demorou muito tempo');
}
```

### Obter Resultados

```javascript
async function getResults(jobId) {
  try {
    const response = await axios.get(`${BASE_URL}/scrape/${jobId}/results`);
    const results = response.data;
    
    console.log(`Perfil: ${results.profile.username}`);
    console.log(`Bio: ${results.profile.bio}`);
    console.log(`Seguidores: ${results.profile.follower_count}`);
    console.log(`Posts: ${results.total_posts}`);
    console.log(`Interações: ${results.total_interactions}`);
    
    return results;
  } catch (error) {
    console.error('Erro:', error.response.data);
  }
}
```

### Script Completo

```javascript
const axios = require('axios');
const fs = require('fs');

const BASE_URL = 'http://localhost:8000/api';

async function scrapeInstagramProfile(username) {
  try {
    // 1. Iniciar scraping
    console.log(`🚀 Iniciando scraping de ${username}...`);
    const startResponse = await axios.post(`${BASE_URL}/scrape`, {
      profile_url: `https://instagram.com/${username}`
    });
    
    const jobId = startResponse.data.id;
    console.log(`✅ Job criado: ${jobId}`);
    
    // 2. Aguardar conclusão
    console.log('⏳ Aguardando conclusão...');
    let jobData;
    while (true) {
      const statusResponse = await axios.get(`${BASE_URL}/scrape/${jobId}`);
      jobData = statusResponse.data;
      
      if (jobData.status === 'completed') {
        console.log('✅ Scraping concluído!');
        break;
      } else if (jobData.status === 'failed') {
        console.error(`❌ Erro: ${jobData.error_message}`);
        return;
      }
      
      console.log(`  Status: ${jobData.status} | Posts: ${jobData.posts_scraped} | Interações: ${jobData.interactions_scraped}`);
      await new Promise(resolve => setTimeout(resolve, 5000));
    }
    
    // 3. Obter resultados
    const resultsResponse = await axios.get(`${BASE_URL}/scrape/${jobId}/results`);
    const results = resultsResponse.data;
    
    // 4. Exibir resultados
    console.log('\n' + '='.repeat(50));
    console.log(`PERFIL: ${results.profile.username}`);
    console.log('='.repeat(50));
    console.log(`Bio: ${results.profile.bio}`);
    console.log(`Privado: ${results.profile.is_private}`);
    console.log(`Seguidores: ${results.profile.follower_count}`);
    console.log(`\nTotal de Posts: ${results.total_posts}`);
    console.log(`Total de Interações: ${results.total_interactions}`);
    
    // 5. Salvar em JSON
    fs.writeFileSync(
      `${username}_results.json`,
      JSON.stringify(results, null, 2)
    );
    
    console.log(`\n✅ Resultados salvos em ${username}_results.json`);
    
  } catch (error) {
    console.error('❌ Erro:', error.message);
  }
}

// Executar
scrapeInstagramProfile('example_user');
```

---

## Tratamento de Erros

### Exemplo: Retry com Backoff

```python
import requests
import time
from typing import Optional

def scrape_with_retry(profile_url: str, max_retries: int = 3) -> Optional[dict]:
    """Tenta raspar com retry automático."""
    
    for attempt in range(max_retries):
        try:
            response = requests.post(
                "http://localhost:8000/api/scrape",
                json={"profile_url": profile_url},
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.Timeout:
            print(f"⏱️ Timeout na tentativa {attempt + 1}/{max_retries}")
        except requests.exceptions.ConnectionError:
            print(f"🔌 Erro de conexão na tentativa {attempt + 1}/{max_retries}")
        except requests.exceptions.HTTPError as e:
            print(f"❌ Erro HTTP: {e}")
            return None
        
        # Backoff exponencial
        if attempt < max_retries - 1:
            wait_time = 2 ** attempt
            print(f"⏳ Aguardando {wait_time} segundos antes de retry...")
            time.sleep(wait_time)
    
    print("❌ Falha após todas as tentativas")
    return None
```

---

## Dicas e Boas Práticas

1. **Sempre verificar o status antes de obter resultados**
2. **Usar paginação para grandes volumes de dados**
3. **Implementar retry com backoff exponencial**
4. **Armazenar job_id para rastrear requisições**
5. **Usar timestamps para controlar atualizações**
6. **Validar URLs antes de enviar**
7. **Monitorar quotas de API (OpenAI)**

---

**Última atualização**: 28 de Janeiro de 2024
