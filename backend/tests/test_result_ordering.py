"""SQL Server rejects duplicate ORDER BY columns, unlike SQLite."""
import pytest
from sqlalchemy.dialects import mssql
from sqlalchemy.orm import Query, Session

from app import main


@pytest.mark.parametrize("sort_by", [
    "row_index", "company_name", "contact_name", "email_address", "final_score", "priority",
])
@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
def test_results_ordering_has_unique_columns(monkeypatch, sort_by, sort_dir):
    captured = []

    def capture(query):
        captured.append(query.statement)
        return []

    monkeypatch.setattr(main, "_get_job_or_404", lambda *args: None)
    monkeypatch.setattr(main, "latest_refreshes", lambda *args: {})
    monkeypatch.setattr(Query, "scalar", lambda query: 0)
    monkeypatch.setattr(Query, "all", capture)
    with Session() as db:
        main.job_results(
            job_id="707ea9a5-1e57-4af4-ab5c-de7a7a02f9c4", db=db,
            details=False, limit=50, offset=0, search=None, country=None,
            industry=None, min_score=None, sort_by=sort_by, sort_dir=sort_dir,
        )
    statement, = captured
    # Compile the actual endpoint query for the production database dialect.
    statement.compile(dialect=mssql.dialect())
    columns = [str(clause.element.compile(dialect=mssql.dialect()))
               for clause in statement._order_by_clauses]
    assert len(columns) == len(set(columns))
    assert columns[-1] == "leads.id"
    assert len(columns) == (2 if sort_by == "row_index" else 4 if sort_by == "priority" else 3)
