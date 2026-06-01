"""Gestione dello stato (watermark).

Persiste l'ultima `sys_updated_on` processata (formato ServiceNow GMT
'YYYY-MM-DD HH:mm:ss'). In cloud su Blob Storage, in locale su file JSON.

- In LETTURA si applica la finestra di sovrapposizione (overlap) per non perdere
  record al bordo; l'upsert idempotente rende la sovrapposizione innocua.
- In SCRITTURA si salva il massimo sys_updated_on effettivamente visto nel run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from .config import StateConfig

logger = logging.getLogger(__name__)

SN_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_sn_datetime(value: str) -> datetime:
    return datetime.strptime(value, SN_DATETIME_FORMAT)


def format_sn_datetime(value: datetime) -> str:
    return value.strftime(SN_DATETIME_FORMAT)


def apply_overlap(watermark: Optional[str], overlap_minutes: int) -> Optional[str]:
    """Arretra il watermark di `overlap_minutes` per la query delta."""
    if not watermark:
        return None
    dt = parse_sn_datetime(watermark) - timedelta(minutes=overlap_minutes)
    return format_sn_datetime(dt)


def max_watermark(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    """Ritorna il massimo tra due watermark in formato SN."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    return candidate if parse_sn_datetime(candidate) > parse_sn_datetime(current) else current


class WatermarkStore:
    """Interfaccia di persistenza del watermark."""

    def read(self) -> Optional[str]:  # pragma: no cover - interfaccia
        raise NotImplementedError

    def write(self, watermark: str) -> None:  # pragma: no cover - interfaccia
        raise NotImplementedError


class LocalFileWatermarkStore(WatermarkStore):
    def __init__(self, path: str) -> None:
        self.path = path

    def read(self) -> Optional[str]:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("watermark")

    def write(self, watermark: str) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"watermark": watermark}, fh)
        logger.info("Watermark salvato su file locale %s: %s", self.path, watermark)


class BlobWatermarkStore(WatermarkStore):
    def __init__(self, connection_string: str, container: str, blob_name: str) -> None:
        from azure.storage.blob import BlobServiceClient  # import lazy

        self._service = BlobServiceClient.from_connection_string(connection_string)
        self._container = container
        self._blob_name = blob_name
        try:
            self._service.create_container(container)
        except Exception:
            # Container gia' esistente: ok.
            pass

    def _client(self):
        return self._service.get_blob_client(self._container, self._blob_name)

    def read(self) -> Optional[str]:
        client = self._client()
        if not client.exists():
            return None
        payload = client.download_blob().readall()
        data = json.loads(payload)
        return data.get("watermark")

    def write(self, watermark: str) -> None:
        client = self._client()
        payload = json.dumps({"watermark": watermark}).encode("utf-8")
        client.upload_blob(payload, overwrite=True)
        logger.info(
            "Watermark salvato su blob %s/%s: %s",
            self._container,
            self._blob_name,
            watermark,
        )


def build_watermark_store(config: StateConfig) -> WatermarkStore:
    """Sceglie il backend: Blob se c'e' la connection string, altrimenti file."""
    if config.blob_connection_string:
        logger.info("Watermark store: Blob Storage (container=%s)", config.blob_container)
        return BlobWatermarkStore(
            config.blob_connection_string, config.blob_container, config.blob_name
        )
    logger.info("Watermark store: file locale (%s)", config.local_path)
    return LocalFileWatermarkStore(config.local_path)
