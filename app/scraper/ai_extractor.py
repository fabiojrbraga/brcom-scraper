"""
Extrator IA Híbrido para processamento de dados do Instagram.
Usa OpenAI Vision + GPT para extrair dados estruturados de screenshots e HTML.
"""

import json
import logging
from typing import Optional, Dict, Any, List
from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger(__name__)


class AIExtractor:
    """
    Extrator que usa IA para processar screenshots e HTML.
    Abordagem híbrida: combina visão computacional com processamento de texto.
    """

    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model_vision = "gpt-4-vision-preview"  # Para análise de imagens
        self.model_text = "gpt-4-mini"  # Para processamento de texto (mais barato)

    async def extract_profile_info(
        self,
        screenshot_base64: Optional[str] = None,
        html_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Extrai informações do perfil a partir de screenshot e/ou HTML.

        Args:
            screenshot_base64: Screenshot do perfil em base64
            html_content: HTML da página do perfil

        Returns:
            Dicionário com informações extraídas
        """
        try:
            logger.info("🧠 Extraindo informações do perfil com IA...")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Analise esta página de perfil do Instagram e extraia:
                            
                            1. Username
                            2. Bio (texto completo)
                            3. Se é conta privada ou pública
                            4. Número de seguidores (se visível)
                            5. Número de seguindo (se visível)
                            6. Número de posts (se visível)
                            7. Se tem verificação azul
                            
                            Retorne APENAS um JSON válido com esta estrutura:
                            {
                                "username": "string",
                                "bio": "string ou null",
                                "is_private": boolean,
                                "follower_count": number ou null,
                                "following_count": number ou null,
                                "post_count": number ou null,
                                "verified": boolean,
                                "confidence": number entre 0 e 1
                            }
                            """,
                        }
                    ],
                }
            ]

            # Adicionar screenshot se disponível
            if screenshot_base64:
                messages[0]["content"].insert(
                    0,
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"},
                    },
                )

            # Adicionar HTML se disponível
            if html_content:
                messages[0]["content"].append(
                    {
                        "type": "text",
                        "text": f"\nHTML da página:\n{html_content[:5000]}",  # Limitar tamanho
                    }
                )

            response = await self.client.chat.completions.create(
                model=self.model_text,
                messages=messages,
                temperature=0.1,  # Baixa temperatura para respostas mais consistentes
            )

            # Extrair JSON da resposta
            response_text = response.choices[0].message.content
            profile_data = json.loads(response_text)

            logger.info(f"✅ Informações do perfil extraídas: {profile_data.get('username')}")
            return profile_data

        except json.JSONDecodeError as e:
            logger.error(f"❌ Erro ao fazer parse do JSON da IA: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Erro ao extrair informações do perfil: {e}")
            raise

    async def extract_posts_info(
        self,
        screenshot_base64: Optional[str] = None,
        html_content: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Extrai informações de posts a partir de screenshot e/ou HTML.

        Args:
            screenshot_base64: Screenshot dos posts em base64
            html_content: HTML contendo os posts

        Returns:
            Lista de dicionários com informações dos posts
        """
        try:
            logger.info("🧠 Extraindo informações dos posts com IA...")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Analise esta página do Instagram e extraia informações de TODOS os posts visíveis:
                            
                            Para cada post, retorne:
                            1. URL do post (link direto)
                            2. Caption/Descrição
                            3. Número de likes
                            4. Número de comentários
                            5. Data do post (se visível)
                            
                            Retorne APENAS um JSON válido com esta estrutura:
                            {
                                "posts": [
                                    {
                                        "post_url": "string",
                                        "caption": "string ou null",
                                        "like_count": number,
                                        "comment_count": number,
                                        "posted_at": "ISO datetime ou null",
                                        "confidence": number entre 0 e 1
                                    }
                                ],
                                "total_posts_visible": number
                            }
                            """,
                        }
                    ],
                }
            ]

            # Adicionar screenshot se disponível
            if screenshot_base64:
                messages[0]["content"].insert(
                    0,
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"},
                    },
                )

            # Adicionar HTML se disponível
            if html_content:
                messages[0]["content"].append(
                    {
                        "type": "text",
                        "text": f"\nHTML da página:\n{html_content[:5000]}",
                    }
                )

            response = await self.client.chat.completions.create(
                model=self.model_text,
                messages=messages,
                temperature=0.1,
            )

            response_text = response.choices[0].message.content
            posts_data = json.loads(response_text)

            logger.info(f"✅ Posts extraídos: {posts_data.get('total_posts_visible', 0)}")
            return posts_data.get("posts", [])

        except json.JSONDecodeError as e:
            logger.error(f"❌ Erro ao fazer parse do JSON da IA: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Erro ao extrair informações dos posts: {e}")
            raise

    async def extract_comments(
        self,
        screenshot_base64: Optional[str] = None,
        html_content: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Extrai comentários de um post.

        Args:
            screenshot_base64: Screenshot dos comentários em base64
            html_content: HTML contendo os comentários

        Returns:
            Lista de dicionários com informações dos comentários
        """
        try:
            logger.info("🧠 Extraindo comentários com IA...")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Analise os comentários nesta imagem/HTML e extraia:
                            
                            Para cada comentário:
                            1. Username de quem comentou
                            2. Texto do comentário (completo)
                            3. Número de likes no comentário
                            4. Número de respostas (se houver)
                            5. Link do perfil do usuário (se possível extrair)
                            
                            Retorne APENAS um JSON válido com esta estrutura:
                            {
                                "comments": [
                                    {
                                        "user_username": "string",
                                        "user_url": "string ou null",
                                        "comment_text": "string",
                                        "comment_likes": number,
                                        "comment_replies": number,
                                        "confidence": number entre 0 e 1
                                    }
                                ],
                                "total_comments_visible": number
                            }
                            """,
                        }
                    ],
                }
            ]

            if screenshot_base64:
                messages[0]["content"].insert(
                    0,
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"},
                    },
                )

            if html_content:
                messages[0]["content"].append(
                    {
                        "type": "text",
                        "text": f"\nHTML:\n{html_content[:5000]}",
                    }
                )

            response = await self.client.chat.completions.create(
                model=self.model_text,
                messages=messages,
                temperature=0.1,
            )

            response_text = response.choices[0].message.content
            comments_data = json.loads(response_text)

            logger.info(f"✅ Comentários extraídos: {comments_data.get('total_comments_visible', 0)}")
            return comments_data.get("comments", [])

        except json.JSONDecodeError as e:
            logger.error(f"❌ Erro ao fazer parse do JSON da IA: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Erro ao extrair comentários: {e}")
            raise

    async def extract_user_info(
        self,
        screenshot_base64: Optional[str] = None,
        html_content: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Extrai informações de um perfil de usuário que interagiu.

        Args:
            screenshot_base64: Screenshot do perfil em base64
            html_content: HTML do perfil
            username: Username do usuário (para contexto)

        Returns:
            Dicionário com informações do usuário
        """
        try:
            logger.info(f"🧠 Extraindo informações do usuário: {username}")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"""Analise o perfil do Instagram do usuário '{username}' e extraia:
                            
                            1. Bio (texto completo)
                            2. Se é conta privada ou pública
                            3. Número de seguidores (se visível)
                            4. Se tem verificação azul
                            
                            Retorne APENAS um JSON válido:
                            {{
                                "bio": "string ou null",
                                "is_private": boolean,
                                "follower_count": number ou null,
                                "verified": boolean,
                                "confidence": number entre 0 e 1
                            }}
                            """,
                        }
                    ],
                }
            ]

            if screenshot_base64:
                messages[0]["content"].insert(
                    0,
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"},
                    },
                )

            if html_content:
                messages[0]["content"].append(
                    {
                        "type": "text",
                        "text": f"\nHTML:\n{html_content[:3000]}",
                    }
                )

            response = await self.client.chat.completions.create(
                model=self.model_text,
                messages=messages,
                temperature=0.1,
            )

            response_text = response.choices[0].message.content
            user_data = json.loads(response_text)

            logger.info(f"✅ Informações do usuário extraídas: {username}")
            return user_data

        except json.JSONDecodeError as e:
            logger.error(f"❌ Erro ao fazer parse do JSON da IA: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Erro ao extrair informações do usuário: {e}")
            raise


# Instância global do extrator
ai_extractor = AIExtractor()
