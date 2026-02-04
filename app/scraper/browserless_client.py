"""
Cliente para integração com Browserless.
Fornece métodos para interagir com navegador headless via API Browserless.
"""

import httpx
import base64
import logging
import asyncio
from typing import Optional, Dict, Any
from config import settings

logger = logging.getLogger(__name__)


class BrowserlessClient:
    """Cliente para comunicação com Browserless."""

    def __init__(self):
        self.host = settings.browserless_host
        self.token = settings.browserless_token
        self.timeout = settings.request_timeout
        self.max_retries = max(1, settings.browserless_request_retries)
        self.retry_backoff_seconds = max(0.1, settings.browserless_retry_backoff_seconds)
        self.semaphore = asyncio.Semaphore(max(1, settings.browserless_max_concurrency))
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    def _is_field_validation_error(self, response: httpx.Response, fields: list[str]) -> bool:
        if response.status_code != 400:
            return False
        try:
            message = response.text or ""
        except Exception:
            return False
        if "not allowed" not in message:
            return False
        return any(f'"{field}" is not allowed' in message for field in fields)

    def _strip_payload_fields(self, payload: Dict[str, Any], fields: list[str]) -> Dict[str, Any]:
        return {key: value for key, value in payload.items() if key not in fields}

    def _safe_response_text(self, response: httpx.Response, limit: int = 600) -> str:
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""
        return text[:limit]

    async def _post_with_retry(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        url_for_log: str,
        fallback_fields: Optional[list[str]] = None,
    ) -> httpx.Response:
        last_exc: Optional[Exception] = None
        full_url = f"{self.host}{endpoint}"

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self.semaphore:
                    response = await self.client.post(
                        full_url,
                        json=payload,
                        headers=self._get_headers(),
                    )

                if response.status_code == 200:
                    return response

                if fallback_fields and self._is_field_validation_error(response, fallback_fields):
                    fallback_payload = self._strip_payload_fields(payload, fallback_fields)
                    async with self.semaphore:
                        response = await self.client.post(
                            full_url,
                            json=fallback_payload,
                            headers=self._get_headers(),
                        )
                    if response.status_code == 200:
                        return response

                retriable_statuses = {408, 429, 500, 502, 503, 504}
                if response.status_code in retriable_statuses and attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff_seconds * attempt)
                    continue

                body = self._safe_response_text(response)
                raise RuntimeError(
                    f"Browserless {endpoint} falhou para {url_for_log} "
                    f"(status={response.status_code}, body={body})"
                )

            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff_seconds * attempt)
                    continue

        if last_exc:
            raise RuntimeError(
                f"Browserless {endpoint} timeout/erro de rede para {url_for_log}: {last_exc}"
            ) from last_exc
        raise RuntimeError(f"Browserless {endpoint} falhou para {url_for_log}")

    async def close(self):
        """Fecha a conexão com Browserless."""
        await self.client.aclose()

    def _get_headers(self) -> Dict[str, str]:
        """Retorna headers para requisições ao Browserless."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def screenshot(
        self,
        url: str,
        full_page: bool = True,
        wait_for: Optional[str] = None,
        timeout: int = 30000,
        cookies: Optional[list[dict]] = None,
        user_agent: Optional[str] = None,
    ) -> str:
        """
        Captura screenshot de uma URL.

        Args:
            url: URL a ser capturada
            full_page: Se True, captura a página inteira
            wait_for: Seletor CSS para esperar antes de capturar
            timeout: Timeout em ms

        Returns:
            Screenshot em base64
        """
        try:
            payload = {
                "url": url,
                "fullPage": full_page,
                "timeout": timeout,
            }

            if wait_for:
                payload["waitFor"] = wait_for
            if cookies:
                payload["cookies"] = cookies
            if user_agent:
                payload["userAgent"] = user_agent

            response = await self._post_with_retry(
                endpoint="/screenshot",
                payload=payload,
                url_for_log=url,
                fallback_fields=["fullPage", "timeout", "cookies", "userAgent"],
            )

            # Alguns Browserless retornam JSON com base64, outros retornam bytes da imagem.
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                screenshot_data = response.json().get("data")
            else:
                screenshot_data = base64.b64encode(response.content).decode("ascii")
            logger.info(f"✅ Screenshot capturado: {url}")
            return screenshot_data

        except Exception as e:
            logger.error(f"❌ Erro ao capturar screenshot de {url}: {e}")
            raise

    async def get_html(
        self,
        url: str,
        wait_for: Optional[str] = None,
        timeout: int = 30000,
        cookies: Optional[list[dict]] = None,
        user_agent: Optional[str] = None,
    ) -> str:
        """
        Obtém HTML de uma URL.

        Args:
            url: URL a ser acessada
            wait_for: Seletor CSS para esperar antes de retornar
            timeout: Timeout em ms

        Returns:
            HTML da página
        """
        try:
            payload = {
                "url": url,
                "timeout": timeout,
            }

            if wait_for:
                payload["waitFor"] = wait_for
            if cookies:
                payload["cookies"] = cookies
            if user_agent:
                payload["userAgent"] = user_agent

            response = await self._post_with_retry(
                endpoint="/content",
                payload=payload,
                url_for_log=url,
                fallback_fields=["timeout", "cookies", "userAgent"],
            )

            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                try:
                    html = response.json().get("data")
                except ValueError:
                    html = response.text
            else:
                html = response.text
            logger.info(f"✅ HTML obtido: {url}")
            return html

        except Exception as e:
            logger.error(f"❌ Erro ao obter HTML de {url}: {e}")
            raise

    async def execute_script(
        self,
        url: str,
        script: str,
        timeout: int = 30000,
    ) -> Any:
        """
        Executa JavaScript em uma página.

        Args:
            url: URL da página
            script: Código JavaScript a executar
            timeout: Timeout em ms

        Returns:
            Resultado da execução
        """
        try:
            payload = {
                "url": url,
                "code": script,
                "timeout": timeout,
            }

            response = await self._post_with_retry(
                endpoint="/execute",
                payload=payload,
                url_for_log=url,
                fallback_fields=["timeout"],
            )
            result = response.json().get("data")
            logger.info(f"✅ Script executado em: {url}")
            return result

        except Exception as e:
            logger.error(f"❌ Erro ao executar script em {url}: {e}")
            raise

    async def pdf(
        self,
        url: str,
        timeout: int = 30000,
    ) -> bytes:
        """
        Gera PDF de uma URL.

        Args:
            url: URL a ser convertida
            timeout: Timeout em ms

        Returns:
            PDF em bytes
        """
        try:
            payload = {
                "url": url,
                "timeout": timeout,
            }

            response = await self._post_with_retry(
                endpoint="/pdf",
                payload=payload,
                url_for_log=url,
                fallback_fields=["timeout"],
            )
            pdf_data = response.content
            logger.info(f"✅ PDF gerado: {url}")
            return pdf_data

        except Exception as e:
            logger.error(f"❌ Erro ao gerar PDF de {url}: {e}")
            raise

    async def health_check(self) -> bool:
        """
        Verifica se Browserless está acessível.

        Returns:
            True se acessível, False caso contrário
        """
        try:
            response = await self.client.get(
                f"{self.host}/health",
                headers=self._get_headers(),
            )
            is_healthy = response.status_code == 200
            status = "✅ Saudável" if is_healthy else "❌ Indisponível"
            logger.info(f"Browserless status: {status}")
            return is_healthy
        except Exception as e:
            logger.error(f"❌ Erro ao verificar saúde do Browserless: {e}")
            return False
