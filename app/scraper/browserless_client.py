"""
Cliente para integração com Browserless.
Fornece métodos para interagir com navegador headless via API Browserless.
"""

import httpx
import base64
import logging
from typing import Optional, Dict, Any
from config import settings

logger = logging.getLogger(__name__)


class BrowserlessClient:
    """Cliente para comunicação com Browserless."""

    def __init__(self):
        self.host = settings.browserless_host
        self.token = settings.browserless_token
        self.timeout = settings.request_timeout
        self.client = httpx.AsyncClient(timeout=self.timeout)

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

            response = await self.client.post(
                f"{self.host}/screenshot",
                json=payload,
                headers=self._get_headers(),
            )

            if response.status_code == 200:
                # Alguns Browserless retornam JSON com base64, outros retornam bytes da imagem.
                content_type = response.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    screenshot_data = response.json().get("data")
                else:
                    screenshot_data = base64.b64encode(response.content).decode("ascii")
                logger.info(f"✅ Screenshot capturado: {url}")
                return screenshot_data

            if self._is_field_validation_error(response, ["fullPage", "timeout"]):
                fallback_payload = self._strip_payload_fields(payload, ["fullPage", "timeout"])
                response = await self.client.post(
                    f"{self.host}/screenshot",
                    json=fallback_payload,
                    headers=self._get_headers(),
                )
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "").lower()
                    if "application/json" in content_type:
                        screenshot_data = response.json().get("data")
                    else:
                        screenshot_data = base64.b64encode(response.content).decode("ascii")
                    logger.info(f"✅ Screenshot capturado (fallback): {url}")
                    return screenshot_data

            logger.error(f"❌ Erro ao capturar screenshot: {response.text}")
            raise Exception(f"Browserless error: {response.text}")

        except Exception as e:
            logger.error(f"❌ Erro ao capturar screenshot de {url}: {e}")
            raise

    async def get_html(
        self,
        url: str,
        wait_for: Optional[str] = None,
        timeout: int = 30000,
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

            response = await self.client.post(
                f"{self.host}/content",
                json=payload,
                headers=self._get_headers(),
            )

            if response.status_code == 200:
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

            if self._is_field_validation_error(response, ["timeout"]):
                fallback_payload = self._strip_payload_fields(payload, ["timeout"])
                response = await self.client.post(
                    f"{self.host}/content",
                    json=fallback_payload,
                    headers=self._get_headers(),
                )
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "").lower()
                    if "application/json" in content_type:
                        try:
                            html = response.json().get("data")
                        except ValueError:
                            html = response.text
                    else:
                        html = response.text
                    logger.info(f"✅ HTML obtido (fallback): {url}")
                    return html

            logger.error(f"❌ Erro ao obter HTML: {response.text}")
            raise Exception(f"Browserless error: {response.text}")

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

            response = await self.client.post(
                f"{self.host}/execute",
                json=payload,
                headers=self._get_headers(),
            )

            if response.status_code == 200:
                result = response.json().get("data")
                logger.info(f"✅ Script executado em: {url}")
                return result

            if self._is_field_validation_error(response, ["timeout"]):
                fallback_payload = self._strip_payload_fields(payload, ["timeout"])
                response = await self.client.post(
                    f"{self.host}/execute",
                    json=fallback_payload,
                    headers=self._get_headers(),
                )
                if response.status_code == 200:
                    result = response.json().get("data")
                    logger.info(f"✅ Script executado (fallback): {url}")
                    return result

            logger.error(f"❌ Erro ao executar script: {response.text}")
            raise Exception(f"Browserless error: {response.text}")

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

            response = await self.client.post(
                f"{self.host}/pdf",
                json=payload,
                headers=self._get_headers(),
            )

            if response.status_code == 200:
                pdf_data = response.content
                logger.info(f"✅ PDF gerado: {url}")
                return pdf_data

            if self._is_field_validation_error(response, ["timeout"]):
                fallback_payload = self._strip_payload_fields(payload, ["timeout"])
                response = await self.client.post(
                    f"{self.host}/pdf",
                    json=fallback_payload,
                    headers=self._get_headers(),
                )
                if response.status_code == 200:
                    pdf_data = response.content
                    logger.info(f"✅ PDF gerado (fallback): {url}")
                    return pdf_data

            logger.error(f"❌ Erro ao gerar PDF: {response.text}")
            raise Exception(f"Browserless error: {response.text}")

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
