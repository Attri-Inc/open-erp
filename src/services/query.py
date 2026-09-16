"""Ad-hoc read-only SQL, for the questions no purpose-built tool anticipates."""

from src.repositories.protocols import QueryRunner


class QueryService:
    def __init__(self, runner: QueryRunner):
        self._runner = runner

    async def select(self, sql: str, limit: int = 200) -> dict:
        rows = await self._runner.select(sql)
        return {"rows": rows[:limit], "returned": min(len(rows), limit), "total": len(rows)}
