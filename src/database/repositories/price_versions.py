"""Persistence operations used by the P1-007 confirmation transaction."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from src.models import Channel, StandardPrice


class ProductMappingConflictError(ValueError):
    """Raised when an existing Google product points at another tier."""


class PriceVersionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._last_archived_version_id: str | None = None

    def get(self, version_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM price_versions WHERE version_id = ?", (version_id,)
        ).fetchone()

    def get_active(self, channel: Channel) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM price_versions
            WHERE channel = ? AND status = 'ACTIVE'
            """,
            (channel.value,),
        ).fetchone()

    def active_coverage(self, channel: Channel) -> tuple[str | None, int, int]:
        row = self._connection.execute(
            """
            SELECT v.version_id,
                   COUNT(p.id) AS record_count,
                   COUNT(DISTINCT p.country_code) AS country_count
            FROM price_versions AS v
            LEFT JOIN channel_prices AS p
              ON p.version_id = v.version_id AND p.channel = v.channel
            WHERE v.channel = ? AND v.status = 'ACTIVE'
            GROUP BY v.version_id
            """,
            (channel.value,),
        ).fetchone()
        if row is None:
            return None, 0, 0
        return str(row["version_id"]), int(row["country_count"]), int(row["record_count"])

    def find_by_source_sha256(self, channel: Channel, source_sha256: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM price_versions
            WHERE channel = ? AND source_sha256 = ?
            ORDER BY import_time DESC, version_id DESC
            LIMIT 1
            """,
            (channel.value, source_sha256),
        ).fetchone()

    def next_version_id(self, channel: Channel, local_date: date) -> str:
        prefix = f"{channel.value}_V{local_date:%Y%m%d}_"
        rows = self._connection.execute(
            "SELECT version_id FROM price_versions WHERE version_id LIKE ?",
            (f"{prefix}%",),
        )
        pattern = re.compile(rf"^{re.escape(prefix)}([0-9]{{3}})$")
        sequences = [
            int(match.group(1))
            for row in rows
            if (match := pattern.fullmatch(str(row["version_id"]))) is not None
        ]
        sequence = max(sequences, default=0) + 1
        if sequence > 999:
            raise OverflowError(f"daily version sequence is exhausted for {channel.value}")
        return f"{prefix}{sequence:03d}"

    def archive_active(self, channel: Channel) -> None:
        current = self.get_active(channel)
        self._last_archived_version_id = (
            str(current["version_id"]) if current is not None else None
        )
        self._connection.execute(
            """
            UPDATE price_versions
            SET status = 'ARCHIVED'
            WHERE channel = ? AND status = 'ACTIVE'
            """,
            (channel.value,),
        )

    def create_active(
        self,
        *,
        version_id: str,
        channel: Channel,
        source_file: str,
        source_sha256: str,
        import_time: str,
        record_count: int,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO price_versions
                (version_id, channel, source_file, source_sha256,
                 import_time, status, record_count)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (
                version_id,
                channel.value,
                source_file,
                source_sha256,
                import_time,
                record_count,
            ),
        )
        if self._last_archived_version_id is not None:
            self._add_import_event(
                version_id=self._last_archived_version_id,
                channel=channel,
                from_status="ACTIVE",
                to_status="ARCHIVED",
                replaced_version_id=version_id,
                created_time=import_time,
            )
        self._add_import_event(
            version_id=version_id,
            channel=channel,
            from_status=None,
            to_status="ACTIVE",
            replaced_version_id=self._last_archived_version_id,
            created_time=import_time,
        )

    def add_prices(
        self,
        *,
        version_id: str,
        records: Iterable[StandardPrice],
        created_time: str,
    ) -> int:
        values = tuple(
            (
                record.channel.value,
                record.country_code,
                str(record.usd_tier),
                record.currency,
                str(record.local_price),
                record.adjustment_mode.value if record.adjustment_mode is not None else None,
                version_id,
                created_time,
            )
            for record in records
        )
        self._connection.executemany(
            """
            INSERT INTO channel_prices
                (channel, country_code, usd_tier, currency, local_price,
                 adjustment_mode, version_id, created_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return len(values)

    def synchronize_google_products(self, records: Iterable[StandardPrice]) -> None:
        mappings: dict[str, Decimal] = {}
        for record in records:
            if record.channel is not Channel.GOOGLE or record.product_id is None:
                continue
            tier = Decimal(str(record.usd_tier))
            previous = mappings.get(record.product_id)
            if previous is not None and previous != tier:
                raise ProductMappingConflictError(
                    f"Google product {record.product_id!r} maps to multiple tiers"
                )
            mappings[record.product_id] = tier

        for product_id, tier in sorted(mappings.items()):
            existing = self._connection.execute(
                """
                SELECT usd_tier FROM channel_products
                WHERE channel = 'GOOGLE' AND product_id = ?
                """,
                (product_id,),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO channel_products (channel, product_id, usd_tier)
                    VALUES ('GOOGLE', ?, ?)
                    """,
                    (product_id, str(tier)),
                )
                continue
            existing_tier = Decimal(str(existing["usd_tier"]))
            if existing_tier != tier:
                raise ProductMappingConflictError(
                    f"Google product {product_id!r} already maps to tier {existing_tier}"
                )

    def _add_import_event(
        self,
        *,
        version_id: str,
        channel: Channel,
        from_status: str | None,
        to_status: str,
        replaced_version_id: str | None,
        created_time: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO version_status_events
                (event_id, version_id, channel, from_status, to_status,
                 replaced_version_id, reason, note, actor, created_time)
            VALUES (?, ?, ?, ?, ?, ?, 'IMPORT_CONFIRMATION', NULL,
                    'LOCAL_USER', ?)
            """,
            (
                f"EVT_{uuid.uuid4().hex.upper()}",
                version_id,
                channel.value,
                from_status,
                to_status,
                replaced_version_id,
                created_time,
            ),
        )
