"""
Configuration and settings for Job Agent.

Loads environment variables and provides centralized config.
"""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Application metadata
    app_name: str = "Job Agent"
    app_version: str = "0.1.0"
    environment: str = "development"  # development, testing, production
    
    # Database
    database_url: Optional[str] = None  # Falls back to ~/Library/.../job_agent.db
    database_path: Optional[Path] = None  # Explicit override for db_path
    
    # Playwright browser automation
    browser_type: str = "chromium"  # chromium, firefox, webkit
    headless: bool = False  # Show browser windows (required for MFA/CAPTCHA)
    browser_timeout: int = 30000  # ms
    navigation_timeout: int = 30000  # ms
    
    # Rate limiting & pacing (§0)
    request_delay_min: float = 1.0  # Minimum delay between actions (seconds)
    request_delay_max: float = 5.0  # Maximum randomized delay (seconds)
    search_batch_delay: float = 2.0  # Delay between search result pages (seconds)
    application_delay: float = 3.0  # Delay between applications (seconds)
    email_rate_limit: int = 5  # Max emails per hour
    
    # Automation thresholds
    clean_submissions_threshold: int = 3  # Must manually review first N submissions before auto-submit
    daily_search_limit: int = 100  # Default max jobs per platform per day
    daily_apply_limit: int = 10  # Default max applications per platform per day
    daily_message_limit: int = 5  # Default max messages per platform per day
    
    # Job matching
    fit_score_threshold: float = 0.6  # 0-1, minimum fit score to consider
    use_ollama: bool = True  # Use Ollama for local LLM (fallback to Claude API if False)
    ollama_url: str = "http://localhost:11434"  # Ollama server URL
    ollama_model: str = "llama3.2"  # Preferred model; falls back to any installed local model
    ollama_allow_cloud_models: bool = False  # Ollama ':cloud' models send data off-machine
    
    # Anthropic API (optional, for better quality fit scoring and tailoring)
    anthropic_api_key: Optional[str] = None  # Optional, from environment
    use_anthropic: bool = False  # Enable Claude API if key is provided
    anthropic_model: str = "claude-sonnet-5"  # Model used for tailoring

    # Pre-submission analysis (Phase 11)
    # The analyst reads the finished application before it is sent. These are
    # floors, not targets: below them the application is not weak, it is one
    # the user would not have sent had they noticed. Everything above a floor
    # is reported and left to the user.
    analyst_enabled: bool = True  # Off only for debugging the pipeline itself
    analyst_min_ats_score: int = 55  # Out of 100; below this no ATS parses it
    analyst_min_fit_score: int = 25  # % of the posting's requirements evidenced

    # Documents (Phase 4)
    document_max_upload_mb: int = 10  # Reject master uploads larger than this
    pdf_engine: str = "auto"  # "auto" | "weasyprint" | "reportlab"
    # WeasyPrint renders through cffi into pango/cairo; a fault there would
    # take down an unattended run. Rendering it in a subprocess turns that into
    # a retryable error, at the cost of a process spawn per document.
    pdf_isolate_weasyprint: bool = True
    
    # Email
    mail_app_enabled: bool = True  # Use macOS Mail.app (preferred; no credentials)
    imap_smtp_enabled: bool = True  # Enable IMAP/SMTP fallback
    mail_send_delay: float = 5.0  # Delay between sending emails (seconds)

    # SMTP/IMAP fallback. Passwords are NEVER stored here — only app-specific
    # passwords in the Keychain, via store_app_password('smtp'|'imap', username, ...)
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None  # The sending address
    imap_host: Optional[str] = None
    imap_port: int = 993
    imap_username: Optional[str] = None  # Defaults to smtp_username
    
    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    dashboard_reload: bool = False  # Auto-reload in development
    
    # Logging
    log_level: str = "INFO"
    log_file: Optional[Path] = None  # If set, logs also to file
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def profile_dir(self) -> Path:
        """Directory for platform profile storage."""
        return Path.home() / "Library" / "Application Support" / "job-agent" / "profiles"
    
    @property
    def documents_dir(self) -> Path:
        """Directory for master documents and generated variants."""
        return Path.home() / "Library" / "Application Support" / "job-agent" / "documents"

    @property
    def db_path(self) -> Path:
        """Path to SQLite database."""
        if self.database_path:
            return self.database_path
        app_support = Path.home() / "Library" / "Application Support" / "job-agent"
        return app_support / "job_agent.db"
    
    @property
    def database_url_computed(self) -> str:
        """Computed database URL if not explicitly set."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path}"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Export singleton
settings = get_settings()
