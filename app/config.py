"""App configuration."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # Paths
    home: Path = Path.home()
    opencode_db: Path = field(
        default_factory=lambda: Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    )
    data_dir: Path = field(
        default_factory=lambda: Path(os.getcwd()) / "data"
    )
    summaries_db: Path = field(default_factory=lambda: Path(os.getcwd()) / "data" / "summaries.db")

    # Scheduling
    poll_interval_minutes: int = 15  # how often to check for new sessions

    # UI
    host: str = "127.0.0.1"
    port: int = 8099

    # Summarization
    max_user_messages_for_summary: int = 5  # include top-N user messages by length

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    discussion_batch_size: int = 10
    discussion_rate_limit_s: float = 1.0
    discussion_max_messages: int = 30
    discussion_summary_version: int = 1

    @property
    def opencode_db_exists(self) -> bool:
        return self.opencode_db.exists()


CONFIG = Config()
