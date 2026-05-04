# Estrutura de dados do sistema

Este documento descreve o schema relacional usado pelo sistema para que um agente LLM consiga montar consultas SQL de analise e relatorios com seguranca.

Fonte de verdade usada neste documento:

- Modelos SQLAlchemy em `app/models.py`.
- Rotinas de inicializacao/compatibilidade em `app/database.py`.
- Banco alvo configurado como PostgreSQL por `DATABASE_URL`.

Observacoes gerais:

- Os IDs sao strings `VARCHAR(36)` contendo UUIDs gerados pela aplicacao.
- As datas sao gravadas pela aplicacao com `datetime.utcnow`; trate como UTC.
- Colunas chamadas `metadata` no banco aparecem no Python como `metadata_json`.
- Para analises, prefira consultas `SELECT`. As tabelas de sessao contem estado de autenticacao e nao devem ser expostas em relatorios.
- O `create_all` do SQLAlchemy cria as tabelas e alguns indices. `app/database.py` tambem garante colunas/indices historicos em `interactions` e `profiles`.

## Visao geral das tabelas

| Tabela | Finalidade principal |
| --- | --- |
| `profiles` | Perfis do Instagram raspados pelo sistema. |
| `posts` | Posts/reels associados a um perfil. |
| `interactions` | Interacoes capturadas em posts/stories, como likes, comentarios, shares, saves e views. |
| `scraping_jobs` | Jobs assincronos de scraping e seus resultados/metadados. |
| `instagram_sessions` | Sessoes autenticadas do Instagram para reutilizacao pelo scraper. |
| `investing_sessions` | Sessoes autenticadas do Investing para reutilizacao pelo scraper. |

## Relacionamentos principais

| Relacionamento | Tipo | Como consultar |
| --- | --- | --- |
| `profiles.id` -> `posts.profile_id` | 1 perfil para N posts | `posts.profile_id = profiles.id` |
| `profiles.id` -> `interactions.profile_id` | 1 perfil para N interacoes | `interactions.profile_id = profiles.id` |
| `posts.id` -> `interactions.post_id` | 1 post para N interacoes | `interactions.post_id = posts.id` |
| `scraping_jobs.profile_url` -> `profiles.instagram_url` | Relacao logica, sem FK | Comparar URLs normalizadas quando necessario. |
| `instagram_sessions` e `investing_sessions` | Sem FK | Usadas para autenticacao, nao para analise de negocio. |

Importante: as delecoes em cascata estao configuradas nos relacionamentos ORM (`cascade="all, delete-orphan"`), mas as foreign keys nao declaram `ON DELETE CASCADE` no banco. Um SQL direto que delete linhas pai pode falhar ou deixar comportamento diferente do ORM.

## `profiles`

Representa um perfil do Instagram conhecido pelo sistema. Deve ser a tabela base para relatorios por perfil, crescimento de audiencia atual, privacidade e verificacao.

Chaves e indices:

- PK: `id`.
- Unique/index: `instagram_username`.
- Index: `full_name`.
- FKs: nenhuma.

| Coluna | Tipo | Chave/indice | Nulo | Default | Descricao |
| --- | --- | --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | PK | Nao | UUID gerado | Identificador interno do perfil. |
| `instagram_username` | `VARCHAR(255)` | Unique, index | Nao | - | Username do Instagram sem `@`; principal chave natural para busca de perfil. |
| `full_name` | `VARCHAR(255)` | Index | Sim | - | Nome exibido no perfil quando extraido. |
| `instagram_url` | `VARCHAR(500)` | - | Nao | - | URL normalizada do perfil. |
| `bio` | `TEXT` | - | Sim | - | Texto da biografia. |
| `is_private` | `BOOLEAN` | - | Sim | `false` | Indica se o perfil era privado no momento da raspagem. |
| `follower_count` | `INTEGER` | - | Sim | - | Quantidade de seguidores capturada no perfil. |
| `following_count` | `INTEGER` | - | Sim | - | Quantidade de perfis seguidos capturada no perfil. |
| `post_count` | `INTEGER` | - | Sim | - | Quantidade de posts informada pelo Instagram no perfil. |
| `profile_picture_url` | `VARCHAR(500)` | - | Sim | - | URL da foto do perfil, quando disponivel. |
| `verified` | `BOOLEAN` | - | Sim | `false` | Indica se o perfil tinha selo de verificacao. |
| `created_at` | `DATETIME` | - | Sim | `utcnow` | Data de criacao do registro interno. |
| `updated_at` | `DATETIME` | - | Sim | `utcnow`, atualiza no update | Data da ultima atualizacao do registro. |
| `last_scraped_at` | `DATETIME` | - | Sim | - | Data da ultima raspagem bem sucedida do perfil. |
| `metadata` | `JSON` | - | Sim | - | Dados extras do perfil. No Python: `metadata_json`. |

Uso recomendado:

- Para buscar um perfil especifico, use `lower(instagram_username) = lower(:username)`.
- `follower_count`, `following_count`, `post_count`, `bio`, `is_private` e `verified` sao snapshots do ultimo scrape, nao historico.
- Nao existe tabela historica de metricas de perfil.

## `posts`

Representa posts ou reels associados a um perfil. Use esta tabela para relatorios de conteudo, engajamento por post e joins com interacoes.

Chaves e indices:

- PK: `id`.
- FK: `profile_id` -> `profiles.id`.
- Index: `profile_id`.
- Index: `post_url`.
- Observacao: o arquivo de modelo contem uma declaracao antiga de `post_url` como unique/not null, mas ela e sobrescrita pela segunda declaracao Python. No metadata SQLAlchemy atual, `post_url` e nullable, indexado e nao unique.

| Coluna | Tipo | Chave/indice | Nulo | Default | Descricao |
| --- | --- | --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | PK | Nao | UUID gerado | Identificador interno do post. |
| `profile_id` | `VARCHAR(36)` | FK, index | Nao | - | Perfil dono do post. Referencia `profiles.id`. |
| `post_url` | `VARCHAR(500)` | Index | Sim | - | URL canonica do post/reel. Usada pela aplicacao para deduplicacao logica. |
| `post_id` | `VARCHAR(255)` | - | Sim | - | ID nativo do Instagram, quando extraido. |
| `caption` | `TEXT` | - | Sim | - | Legenda/texto do post. |
| `like_count` | `INTEGER` | - | Sim | `0` | Quantidade de likes visivel/capturada no post. |
| `comment_count` | `INTEGER` | - | Sim | `0` | Quantidade de comentarios visivel/capturada no post. |
| `share_count` | `INTEGER` | - | Sim | `0` | Quantidade de compartilhamentos quando disponivel. |
| `save_count` | `INTEGER` | - | Sim | `0` | Quantidade de salvamentos quando disponivel. |
| `posted_at` | `DATETIME` | - | Sim | - | Data/hora de publicacao quando extraida e convertida. |
| `created_at` | `DATETIME` | - | Sim | `utcnow` | Data de criacao do registro interno. |
| `updated_at` | `DATETIME` | - | Sim | `utcnow`, atualiza no update | Data da ultima atualizacao do registro. |
| `metadata` | `JSON` | - | Sim | - | Dados extras do post. No Python: `metadata_json`. |

Uso recomendado:

- Para listar posts de um perfil, faca join por `posts.profile_id = profiles.id`.
- Para ordenacao temporal, prefira `posted_at DESC NULLS LAST`; quando `posted_at` for nulo, use `created_at` como fallback.
- Como `post_url` nao e unique no schema atual, use `id` para joins e agregacoes. Use `post_url` para filtros por URL e dedupe apenas quando fizer sentido.

## `interactions`

Representa interacoes de usuarios com posts/stories. E a principal tabela para relatorios de audiencia, usuarios recorrentes, comentarios, curtidas e visualizacoes.

Chaves, FKs, indices e constraints:

- PK: `id`.
- FK: `post_id` -> `posts.id`.
- FK: `profile_id` -> `profiles.id`.
- Index: `post_id`.
- Index: `post_url`.
- Index: `profile_id`.
- Index: `user_username`.
- Unique: `uq_interactions_post_url_user_url_type` em (`post_url`, `user_url`, `interaction_type`).

Observacao sobre a unique: em PostgreSQL, valores `NULL` em `post_url` podem permitir multiplas linhas parecidas. Para dedupe analitico, prefira agrupar por `coalesce(post_url, post_id)`, `user_url` e `interaction_type`.

| Coluna | Tipo | Chave/indice | Nulo | Default | Descricao |
| --- | --- | --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | PK | Nao | UUID gerado | Identificador interno da interacao. |
| `post_id` | `VARCHAR(36)` | FK, index | Nao | - | Post relacionado. Referencia `posts.id`. |
| `post_url` | `VARCHAR(500)` | Index, unique composta | Sim | - | URL do post/story usada para dedupe rapido e filtros por URL. Deve corresponder a `posts.post_url` quando disponivel. |
| `profile_id` | `VARCHAR(36)` | FK, index | Nao | - | Perfil dono do conteudo no qual a interacao ocorreu. Referencia `profiles.id`. |
| `user_username` | `VARCHAR(255)` | Index | Nao | - | Username do usuario que interagiu. Nao e FK para `profiles`. |
| `user_url` | `VARCHAR(500)` | Unique composta | Nao | - | URL do usuario que interagiu. Melhor identificador natural do usuario externo. |
| `user_bio` | `TEXT` | - | Sim | - | Bio do usuario que interagiu, quando o scraper enriquece esse dado. |
| `user_is_private` | `BOOLEAN` | - | Sim | `false` | Indica se o usuario que interagiu era privado no momento da coleta. |
| `user_follower_count` | `INTEGER` | - | Sim | - | Quantidade de seguidores do usuario que interagiu, quando extraida. |
| `interaction_type` | `ENUM interactiontype` | Unique composta | Nao | - | Tipo da interacao: `LIKE`, `COMMENT`, `SHARE`, `SAVE`, `VIEW` no schema atual. |
| `comment_text` | `TEXT` | - | Sim | - | Texto do comentario. Deve ser preenchido apenas para `COMMENT`. |
| `comment_likes` | `INTEGER` | - | Sim | `0` | Likes do comentario, quando disponivel. |
| `comment_replies` | `INTEGER` | - | Sim | `0` | Quantidade de respostas do comentario, quando disponivel. |
| `comment_posted_at` | `VARCHAR(64)` | - | Sim | - | Data relativa/textual do comentario, por exemplo `2 h`; nao e datetime normalizado. |
| `created_at` | `DATETIME` | - | Sim | `utcnow` | Data de criacao do registro interno. |
| `updated_at` | `DATETIME` | - | Sim | `utcnow`, atualiza no update | Data da ultima atualizacao do registro. |
| `metadata` | `JSON` | - | Sim | - | Dados extras da interacao. No Python: `metadata_json`. |

Uso recomendado:

- Para contar interacoes por tipo, use `lower(interaction_type::text)` em PostgreSQL.
- Para comentarios, filtre `lower(interaction_type::text) = 'comment'` e use `comment_text`.
- Para curtidas, filtre `lower(interaction_type::text) = 'like'`.
- Para views de stories, filtre `lower(interaction_type::text) = 'view'`.
- Nao assuma que `user_username` exista em `profiles.instagram_username`. A tabela `profiles` representa perfis raspados como alvo; usuarios que interagiram sao armazenados diretamente em `interactions`.
- Para relatorios de audiencia recorrente, o identificador mais estavel e `user_url`; `user_username` pode mudar.

## `scraping_jobs`

Representa jobs assincronos criados pelos endpoints de scraping. Use esta tabela para auditoria operacional, status, tempos de execucao e diagnostico de falhas.

Chaves e indices:

- PK: `id`.
- FKs: nenhuma.
- Indices explicitos: nenhum.

| Coluna | Tipo | Chave/indice | Nulo | Default | Descricao |
| --- | --- | --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | PK | Nao | UUID gerado | Identificador do job. |
| `profile_url` | `VARCHAR(500)` | - | Nao | - | URL alvo do job. Tambem e usada por jobs genericos/investing como URL alvo. |
| `status` | `VARCHAR(50)` | - | Sim | `pending` | Estado do job. Valores usados: `pending`, `running`, `completed`, `failed`. |
| `started_at` | `DATETIME` | - | Sim | - | Quando o processamento iniciou. |
| `completed_at` | `DATETIME` | - | Sim | - | Quando o processamento terminou, com sucesso ou falha. |
| `error_message` | `TEXT` | - | Sim | - | Erro registrado em jobs com falha ou parcial. |
| `posts_scraped` | `INTEGER` | - | Sim | `0` | Quantidade de posts/story posts processados pelo job. |
| `interactions_scraped` | `INTEGER` | - | Sim | `0` | Quantidade de interacoes processadas pelo job. |
| `created_at` | `DATETIME` | - | Sim | `utcnow` | Data de criacao do job. |
| `metadata` | `JSON` | - | Sim | - | Request, flow, resultado bruto ou payloads auxiliares. No Python: `metadata_json`. |

Uso recomendado:

- Para duracao de job, use `completed_at - started_at` quando ambos existirem.
- Para jobs ainda abertos, `completed_at` sera nulo.
- `metadata` pode conter `request`, `flow`, `result`, `raw_result`, dados de teste e payloads especificos dos endpoints `/scrape`, `/generic_scrape` e `/investing_scrape`.
- Nao existe FK para `profiles`; quando precisar relacionar job e perfil, compare `profile_url` com `profiles.instagram_url` ou extraia o username da URL.

## `instagram_sessions`

Armazena sessoes autenticadas do Instagram. Esta tabela e operacional e sensivel; normalmente nao deve alimentar relatorios de negocio.

Chaves e indices:

- PK: `id`.
- Index: `instagram_username`.
- FKs: nenhuma.

| Coluna | Tipo | Chave/indice | Nulo | Default | Descricao |
| --- | --- | --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | PK | Nao | UUID gerado | Identificador da sessao. |
| `instagram_username` | `VARCHAR(255)` | Index | Sim | - | Username associado a sessao autenticada. |
| `storage_state` | `JSON` | - | Nao | - | Estado do navegador/cookies. Campo sensivel; nao selecione em relatorios. |
| `is_active` | `BOOLEAN` | - | Sim | `true` | Indica se a sessao pode ser reutilizada. |
| `last_used_at` | `DATETIME` | - | Sim | - | Ultima vez em que a sessao foi usada. |
| `created_at` | `DATETIME` | - | Sim | `utcnow` | Data de criacao do registro. |
| `updated_at` | `DATETIME` | - | Sim | `utcnow`, atualiza no update | Data da ultima atualizacao. |

Uso recomendado:

- Para auditoria operacional, consulte apenas `id`, `instagram_username`, `is_active`, `last_used_at`, `created_at`, `updated_at`.
- Nunca inclua `storage_state` em uma resposta de tool para LLM, pois pode conter cookies e credenciais de sessao.

## `investing_sessions`

Armazena sessoes autenticadas do Investing. Assim como `instagram_sessions`, e uma tabela operacional e sensivel.

Chaves e indices:

- PK: `id`.
- Index: `investing_username`.
- FKs: nenhuma.

| Coluna | Tipo | Chave/indice | Nulo | Default | Descricao |
| --- | --- | --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | PK | Nao | UUID gerado | Identificador da sessao. |
| `investing_username` | `VARCHAR(255)` | Index | Sim | - | Username associado a sessao autenticada do Investing. |
| `storage_state` | `JSON` | - | Nao | - | Estado do navegador/cookies. Campo sensivel; nao selecione em relatorios. |
| `is_active` | `BOOLEAN` | - | Sim | `true` | Indica se a sessao pode ser reutilizada. |
| `last_used_at` | `DATETIME` | - | Sim | - | Ultima vez em que a sessao foi usada. |
| `created_at` | `DATETIME` | - | Sim | `utcnow` | Data de criacao do registro. |
| `updated_at` | `DATETIME` | - | Sim | `utcnow`, atualiza no update | Data da ultima atualizacao. |

Uso recomendado:

- Para auditoria operacional, consulte apenas `id`, `investing_username`, `is_active`, `last_used_at`, `created_at`, `updated_at`.
- Nunca inclua `storage_state` em uma resposta de tool para LLM.

## Guia rapido para montar consultas

### Perfil com posts e metricas

Use `profiles` como tabela base, faca `LEFT JOIN posts` para manter perfis sem posts.

```sql
SELECT
  p.instagram_username,
  p.full_name,
  p.follower_count,
  COUNT(po.id) AS total_posts_salvos,
  COALESCE(SUM(po.like_count), 0) AS total_likes_salvos,
  COALESCE(SUM(po.comment_count), 0) AS total_comentarios_salvos
FROM profiles p
LEFT JOIN posts po ON po.profile_id = p.id
WHERE lower(p.instagram_username) = lower(:username)
GROUP BY p.id, p.instagram_username, p.full_name, p.follower_count;
```

### Interacoes por tipo em um perfil

```sql
SELECT
  lower(i.interaction_type::text) AS tipo,
  COUNT(*) AS total
FROM profiles p
JOIN interactions i ON i.profile_id = p.id
WHERE lower(p.instagram_username) = lower(:username)
GROUP BY lower(i.interaction_type::text)
ORDER BY total DESC;
```

### Usuarios que mais interagem com um perfil

```sql
SELECT
  i.user_url,
  MAX(i.user_username) AS user_username,
  COUNT(*) AS total_interacoes,
  COUNT(*) FILTER (WHERE lower(i.interaction_type::text) = 'like') AS likes,
  COUNT(*) FILTER (WHERE lower(i.interaction_type::text) = 'comment') AS comentarios,
  COUNT(*) FILTER (WHERE lower(i.interaction_type::text) = 'view') AS views
FROM profiles p
JOIN interactions i ON i.profile_id = p.id
WHERE lower(p.instagram_username) = lower(:username)
GROUP BY i.user_url
ORDER BY total_interacoes DESC
LIMIT :limit;
```

### Comentarios coletados por post

```sql
SELECT
  p.instagram_username,
  po.post_url,
  i.user_username,
  i.user_url,
  i.comment_text,
  i.comment_likes,
  i.comment_replies,
  i.comment_posted_at,
  i.created_at AS coletado_em
FROM interactions i
JOIN posts po ON po.id = i.post_id
JOIN profiles p ON p.id = i.profile_id
WHERE lower(i.interaction_type::text) = 'comment'
  AND i.comment_text IS NOT NULL
  AND lower(p.instagram_username) = lower(:username)
ORDER BY i.created_at DESC;
```

### Posts mais fortes por engajamento salvo

```sql
SELECT
  p.instagram_username,
  po.post_url,
  po.caption,
  po.posted_at,
  po.like_count,
  po.comment_count,
  po.share_count,
  po.save_count,
  (COALESCE(po.like_count, 0)
    + COALESCE(po.comment_count, 0)
    + COALESCE(po.share_count, 0)
    + COALESCE(po.save_count, 0)) AS engajamento_total
FROM posts po
JOIN profiles p ON p.id = po.profile_id
WHERE lower(p.instagram_username) = lower(:username)
ORDER BY engajamento_total DESC, po.posted_at DESC NULLS LAST
LIMIT :limit;
```

### Audiencia em comum entre perfis

```sql
SELECT
  i.user_url,
  MAX(i.user_username) AS user_username,
  COUNT(DISTINCT p.id) AS perfis_distintos,
  ARRAY_AGG(DISTINCT p.instagram_username ORDER BY p.instagram_username) AS perfis
FROM interactions i
JOIN profiles p ON p.id = i.profile_id
WHERE lower(p.instagram_username) = ANY(:usernames_lower)
GROUP BY i.user_url
HAVING COUNT(DISTINCT p.id) > 1
ORDER BY perfis_distintos DESC, user_username;
```

Para o parametro `:usernames_lower`, envie um array de usernames ja normalizados para lower-case, por exemplo `ARRAY['perfil_a', 'perfil_b']`.

### Saude operacional dos jobs

```sql
SELECT
  status,
  COUNT(*) AS total_jobs,
  AVG(EXTRACT(EPOCH FROM (completed_at - started_at))) AS duracao_media_segundos,
  SUM(posts_scraped) AS posts_scraped,
  SUM(interactions_scraped) AS interactions_scraped
FROM scraping_jobs
WHERE created_at >= :inicio
GROUP BY status
ORDER BY total_jobs DESC;
```

## Boas praticas para o agente SQL

- Gere consultas somente leitura (`SELECT`) para analises e relatorios.
- Sempre qualifique colunas com alias quando usar joins (`p`, `po`, `i`, `j`).
- Use `profiles.id`, `posts.id` e `interactions.id` como chaves tecnicas; use usernames/URLs apenas para filtro e apresentacao.
- Ao filtrar `interaction_type`, prefira `lower(interaction_type::text)` para tolerar bases antigas com enum em caixa diferente.
- Evite `SELECT *`, principalmente em tabelas com `metadata` e `storage_state`.
- Nunca selecione `instagram_sessions.storage_state` ou `investing_sessions.storage_state` para um LLM.
- Para rankings de usuarios, agrupe por `user_url` e use `MAX(user_username)` apenas como rotulo.
- Para posts, faca join com `interactions` por `interactions.post_id = posts.id`; `post_url` deve ser usado como apoio, nao como chave primaria.
- Campos de contagem extraidos do Instagram podem estar nulos ou desatualizados. Use `COALESCE(campo, 0)` em somas.
- `comment_posted_at` e textual/relativo, nao serve para filtros cronologicos confiaveis. Para data de coleta, use `interactions.created_at`.
