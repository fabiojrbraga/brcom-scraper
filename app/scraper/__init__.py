"""
Módulo de scraping do Instagram.
Contém integrações com Browserless, Browser Use e IA Generativa.
"""

from app.scraper.browserless_client import BrowserlessClient
from app.scraper.browser_use_agent import BrowserUseAgent
from app.scraper.ai_extractor import AIExtractor

__all__ = ["BrowserlessClient", "BrowserUseAgent", "AIExtractor"]
