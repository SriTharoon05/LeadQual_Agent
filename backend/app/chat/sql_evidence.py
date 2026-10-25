"""Fixed, guarded SQL expressions for the same sourced facts shown in proofs."""
from sqlalchemy import Integer, UnicodeText, and_, case, cast, exists, func, literal_column, or_, select

from app.models import Lead, ValidationResult

FACTS = {"employee_count", "annual_revenue", "location", "industry", "seniority", "employment"}


def _text(value):
    # Called only with constants/allowlisted JSON paths in this module. User or
    # model strings never enter literal SQL; their filters remain bind parameters.
    return literal_column("N'" + value.replace("'", "''") + "'")


def _trim_url(value):
    trimmed = func.ltrim(func.rtrim(value))
    # Match str.strip().rstrip('/') without requiring SQL Server 2022 RTRIM args.
    return func.left(trimmed, func.len(trimmed) - func.patindex(_text("%[^/]%"), func.reverse(trimmed)) + 1).collate("Latin1_General_100_BIN2")


def _safe_http_url(url):
    remainder = func.substring(url, func.charindex(_text("://"), url) + 3, literal_column("2048"))
    boundary = func.replace(func.replace(remainder, _text("?"), _text("/")), _text("#"), _text("/"))
    authority = func.left(boundary, func.charindex(_text("/"), boundary + _text("/")) - 1)
    opening = func.charindex(_text("["), authority)
    closing = func.charindex(_text("]"), authority)
    brackets = or_(and_(opening == literal_column("0"), closing == literal_column("0")),
                   and_(opening == literal_column("1"), closing > literal_column("2")))
    whitespace = [func.charindex(func.nchar(literal_column(str(code))), authority) == literal_column("0")
                  for code in (9, 10, 13, 32)]
    return and_(func.len(url) <= literal_column("2048"), func.len(authority) > literal_column("0"),
        func.charindex(_text("@"), authority) == literal_column("0"),
        brackets, *whitespace,
        or_(func.lower(url).like(_text("http://%")), func.lower(url).like(_text("https://%"))))


def _excerpt_text(value):
    for code in (9, 10, 11, 12, 13, 160):
        value = func.replace(value, func.nchar(literal_column(str(code))), _text(" "))
    return func.ltrim(func.rtrim(value))


def _array(payload, path):
    value = func.json_query(payload, _text(path))
    return case((func.left(func.ltrim(value), literal_column("1")) == _text("["), value), else_=_text("[]"))


def confirmed_fact_value(fact_name, lead=Lead):
    """NULL unless a confirmed core fact has an attributed usable excerpt.

    Uses the unique/indexed (lead_id,stage) research row. Every JSON rowset is
    guarded against malformed/scalar legacy JSON. Source arrays are bounded to
    the display contract's first five proof/30 tool entries.
    """
    if fact_name not in FACTS:
        raise ValueError("Unsupported researched fact")
    raw = select(cast(ValidationResult.raw_output, UnicodeText)).where(
        ValidationResult.lead_id == lead.id, ValidationResult.stage == "web").correlate(lead).scalar_subquery()
    payload = case((func.isjson(raw) == literal_column("1"), raw), else_=_text("{}"))
    path = "$.evidence." + fact_name
    proof = func.openjson(_array(payload, path + ".sources")).table_valued("key", "value", "type").alias("fact_sources")
    tool = func.openjson(_array(payload, "$.sources")).table_valued("key", "value", "type").alias("tool_sources")
    source_json = case((proof.c.type == literal_column("5"), proof.c.value), else_=_text("{}"))
    url = func.ltrim(func.rtrim(func.json_value(source_json, _text("$.url"))))
    properties = func.openjson(source_json).table_valued("key", "value", "type").alias("source_properties")
    excerpt = exists(select(literal_column("1")).select_from(properties).where(
        properties.c.key == _text("excerpt"), properties.c.type == literal_column("1"),
        func.len(_excerpt_text(properties.c.value)) > literal_column("0")).correlate(proof))
    linked = exists(select(literal_column("1")).select_from(proof.join(tool,
        _trim_url(url) == _trim_url(tool.c.value))).where(
        proof.c.type == literal_column("5"), cast(proof.c.key, Integer) < literal_column("5"),
        tool.c.type == literal_column("1"), cast(tool.c.key, Integer) < literal_column("30"),
        _safe_http_url(url), _safe_http_url(tool.c.value), excerpt).correlate(lead))
    value = func.left(func.ltrim(func.rtrim(func.json_value(payload, _text(path + ".value")))), literal_column("500"))
    supported = and_(func.json_value(payload, _text(path + ".status")).collate("Latin1_General_100_BIN2") == _text("confirmed"),
                     value.isnot(None), func.len(value) > literal_column("0"), linked)
    if fact_name == "employment":
        supported = and_(supported, value.collate("Latin1_General_100_BIN2").in_((_text("current"), _text("former"))))
    return case((supported, value), else_=None)
