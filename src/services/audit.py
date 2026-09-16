"""Audit reads. Every mutation writes its audit row inside the same Unit of
Work as the change, so the trail can never run ahead of or behind the data."""

from src.repositories.protocols import AuditReader


class AuditService:
    def __init__(self, reader: AuditReader):
        self._reader = reader

    async def list(
        self,
        action: str | None = None,
        actor: str | None = None,
        object_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        rows, total = await self._reader.list(action, actor, object_id, limit, offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset}
