"""
utils/company_logger.py
───────────────────────
Per-company file logger for DEEP pipeline debugging. (HARDENED edition)

Creates one log file per company under the root-level `Logs/` folder, capturing
the FULL raw output (JSON) of every pipeline stage for a single lead:

    Stage 1 ICP  →  Stage 2 Web  →  Website Scrape  →  AI Enrich
                 →  Stage 3 Contact  →  Scoring

New fields added anywhere in the pipeline (e.g. ICP `major_disqualifiers`) are
captured automatically, because stage results are serialised via to_jsonable()
which dumps the whole object.

Production hardening in this edition:
  • to_jsonable() has a RECURSION-DEPTH guard + cycle detection, so a circular
    reference can never cause a stack-overflow crash.
  • _dumps() caps output length so a giant LLM raw_output can't produce
    multi-MB log lines / runaway files.
  • Collections are capped; every path is exception-safe.
  • scrape() coerces a non-dict `research` safely.

Each stage also records the SOURCE .py FILE that produced the result. Output is
echoed to stdout AND written to a per-company file.
"""

import inspect
import json
import os
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone

# Root-level Logs directory  ( <project_root>/Logs )
LOGS_DIR = os.path.join(os.getcwd(), "Logs")

# India Standard Time (UTC+5:30) — fixed offset, no external tz library needed.
IST = timezone(timedelta(hours=5, minutes=30))

_dir_lock = threading.Lock()

# Serialisation safety limits.
_MAX_DEPTH        = 12       # recursion guard for to_jsonable
_MAX_ITEMS        = 500      # cap list/tuple/set length
_MAX_DICT_KEYS    = 500      # cap dict size
_MAX_DUMP_CHARS   = 200_000  # cap a single serialised block (~200 KB)


# ── Filesystem helpers ────────────────────────────────────────────────────────
def _ensure_logs_dir() -> str:
    try:
        with _dir_lock:
            os.makedirs(LOGS_DIR, exist_ok=True)
    except Exception:
        pass
    return LOGS_DIR


def _job_log_dir(job_id) -> str:
    """
    Return (creating if needed) the per-job folder that holds all company logs:
        Logs/<dd-mm-yyyy_HH-MM>_IST - <jobID>/
    ONE folder per job — reuse an existing folder ending with this job id so all
    companies land together even though leads run in parallel.
    """
    _ensure_logs_dir()
    jid = str(job_id) if job_id else "no-job"
    try:
        with _dir_lock:
            for name in os.listdir(LOGS_DIR):
                full = os.path.join(LOGS_DIR, name)
                if os.path.isdir(full) and name.endswith(f"- {jid}"):
                    return full
            stamp = datetime.now(IST).strftime("%d-%m-%Y_%H-%M")
            folder = os.path.join(LOGS_DIR, f"{stamp}_IST - {jid}")
            os.makedirs(folder, exist_ok=True)
            return folder
    except Exception:
        # Worst case, fall back to the root Logs dir so logging still works.
        return LOGS_DIR


def _sanitize(name) -> str:
    """Make a filesystem-safe file stem from a company name."""
    if not name or not str(name).strip():
        return "unknown_company"
    stem = str(name).strip()
    stem = re.sub(r"[^\w\-. ]+", "_", stem)   # drop odd chars -> _
    stem = re.sub(r"\s+", "_", stem)          # spaces -> _
    stem = stem.strip("._")
    return stem[:120] or "unknown_company"


# ── Source-file resolution ────────────────────────────────────────────────────
def resolve_source(obj) -> str:
    """
    Figure out WHICH .py file produced a result, for traceability.
    Accepts a callable, a string (returned as-is), or a result object.
    Returns e.g. 'app/validators/icp_validator.py', or '-' if unknown.
    Never raises.
    """
    if obj is None:
        return "-"
    if isinstance(obj, str):
        return obj

    module = None
    try:
        if callable(obj) or inspect.ismodule(obj):
            module = inspect.getmodule(obj)
        else:
            module = inspect.getmodule(type(obj))
    except Exception:
        module = None

    try:
        src_file = inspect.getsourcefile(obj) if (callable(obj) or inspect.ismodule(obj)) \
            else inspect.getsourcefile(type(obj))
        if src_file:
            src_file = src_file.replace("\\", "/")
            marker = "/app/"
            if marker in src_file:
                return "app/" + src_file.split(marker, 1)[1]
            return os.path.basename(src_file)
    except Exception:
        pass

    if module is not None:
        try:
            name = getattr(module, "__name__", None)
            if name:
                return name.replace(".", "/") + ".py"
        except Exception:
            pass
    return "-"


# ── Serialisation (recursion + cycle safe) ────────────────────────────────────
def to_jsonable(obj, _depth: int = 0, _seen: set | None = None):
    """
    Best-effort conversion of ANY object to a JSON-serialisable structure.
    Handles pydantic v1/v2 models, dataclasses, ORM-ish objects, dicts, lists.

    Hardened:
      • depth-limited (prevents stack overflow on deeply nested / recursive data)
      • cycle-detected via object ids (prevents infinite loops)
      • collections capped in length
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if _depth >= _MAX_DEPTH:
        return "…(max depth)"

    if _seen is None:
        _seen = set()
    oid = id(obj)
    # Only track containers/objects (not immutables) for cycle detection.
    if not isinstance(obj, (str, bytes, int, float, bool)):
        if oid in _seen:
            return "…(circular)"
        _seen = _seen | {oid}

    # Pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return to_jsonable(obj.model_dump(), _depth + 1, _seen)
        except Exception:
            pass

    # Pydantic v1 / anything exposing .dict()
    dict_attr = getattr(obj, "dict", None)
    if callable(dict_attr):
        try:
            return to_jsonable(dict_attr(), _depth + 1, _seen)
        except Exception:
            pass

    # Mapping
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= _MAX_DICT_KEYS:
                out["…"] = f"(+{len(obj) - _MAX_DICT_KEYS} more keys)"
                break
            try:
                out[str(k)] = to_jsonable(v, _depth + 1, _seen)
            except Exception:
                out[str(k)] = "…(unserialisable)"
        return out

    # Iterable
    if isinstance(obj, (list, tuple, set)):
        out = []
        for i, v in enumerate(obj):
            if i >= _MAX_ITEMS:
                out.append(f"…(+{len(obj) - _MAX_ITEMS} more items)")
                break
            try:
                out.append(to_jsonable(v, _depth + 1, _seen))
            except Exception:
                out.append("…(unserialisable)")
        return out

    # Generic object with attributes
    if hasattr(obj, "__dict__"):
        out = {}
        try:
            for k, v in vars(obj).items():
                if k.startswith("_"):
                    continue
                try:
                    out[k] = to_jsonable(v, _depth + 1, _seen)
                except Exception:
                    out[k] = "…(unserialisable)"
        except Exception:
            return str(obj)
        return out

    try:
        return str(obj)
    except Exception:
        return "…(unserialisable)"


def _dumps(obj) -> str:
    """Serialise to pretty JSON, size-capped so it can't bloat the log."""
    try:
        text = json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        try:
            text = f"<unserialisable: {e}> {obj!r}"
        except Exception:
            text = "<unserialisable>"
    if len(text) > _MAX_DUMP_CHARS:
        return text[:_MAX_DUMP_CHARS] + f"\n…(truncated, {len(text)} chars total)"
    return text


# ── The logger ────────────────────────────────────────────────────────────────
class CompanyLogger:
    """
    One instance == one company log file. Thread-safe writes so it plays nicely
    with Celery's threads worker pool. Every public method is exception-safe.
    """

    _write_lock = threading.Lock()

    def __init__(
        self,
        company_name,
        contact_name=None,
        lead_id=None,
        job_id=None,
        icp_prompt=None,
    ) -> None:
        self.company_name = company_name or "Unknown Company"
        self.contact_name = contact_name
        self.lead_id = lead_id
        self.job_id = job_id
        self.icp_prompt = icp_prompt
        self._t0 = datetime.now(IST)

        try:
            job_dir = _job_log_dir(job_id)
            stem = _sanitize(company_name)
            suffix = f"_{str(lead_id)[:8]}" if lead_id else ""
            self.path = os.path.join(job_dir, f"{stem}{suffix}.log")
        except Exception:
            # Fall back to a safe path so logging never crashes construction.
            self.path = os.path.join(_ensure_logs_dir(), "fallback.log")

    # ── low-level write ───────────────────────────────────────────────────────
    def _emit(self, text) -> None:
        try:
            text = text if isinstance(text, str) else str(text)
        except Exception:
            text = "<unprintable>"
        try:
            print(text, flush=True)                    # live console
        except Exception:
            pass
        try:
            with self._write_lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
        except Exception as e:
            try:
                print(f"[CompanyLogger] failed to write {self.path}: {e}", flush=True)
            except Exception:
                pass

    def _rule(self, char: str = "─") -> None:
        self._emit(char * 80)

    # ── public API ────────────────────────────────────────────────────────────
    def header(self, lead_data=None) -> None:
        try:
            self._emit("=" * 80)
            self._emit("LEAD PROCESSING LOG")
            self._emit(f"Company     : {self.company_name}")
            self._emit(f"Contact     : {self.contact_name or '-'}")
            self._emit(f"Lead ID     : {self.lead_id}")
            self._emit(f"Job ID      : {self.job_id}")
            self._emit(f"Started     : {self._t0.isoformat()}")
            if self.icp_prompt:
                self._emit(f"ICP Prompt  : {self.icp_prompt}")
            self._emit("=" * 80)
            if lead_data is not None:
                self._emit("")
                self._emit(">>> INPUT LEAD DATA (raw):")
                self._emit(_dumps(lead_data))
        except Exception as e:
            self._emit(f"[header error: {e}]")

    def stage(self, title, result, source=None, inputs=None, extra=None, started=None) -> None:
        """Log one pipeline stage with its full raw JSON output. Never raises."""
        try:
            self._emit("")
            self._rule()
            self._emit(str(title))
            self._emit(f"validated by : {resolve_source(source if source is not None else result)}")
            self._rule()

            if inputs is not None:
                self._emit(">>> INPUT:")
                self._emit(_dumps(inputs))

            self._emit(">>> RAW OUTPUT (json):")
            self._emit(_dumps(result))

            raw = getattr(result, "raw_output", None)
            if raw:
                self._emit(">>> RAW LLM OUTPUT:")
                self._emit(_dumps(raw) if not isinstance(raw, str) else
                           (raw[:_MAX_DUMP_CHARS] +
                            (f"\n…(truncated, {len(raw)} chars total)" if len(raw) > _MAX_DUMP_CHARS else "")))

            if extra:
                for label, value in (extra.items() if isinstance(extra, dict) else []):
                    self._emit(f">>> {label}:")
                    self._emit(_dumps(value))

            if started:
                self._emit(f">>> duration: {_duration(started):.2f}s")
        except Exception as e:
            self._emit(f"[stage '{title}' error: {e}]")

    # ── Website scrape (RAG ingestion) ────────────────────────────────────────
    def scrape(
        self,
        research,
        clean_text: str | None = None,
        source=None,
        chunk_count: int | None = None,
        blob_path: str | None = None,
        max_chars: int = 4000,
        started=None,
    ) -> None:
        """Log the WEBSITE SCRAPE + PARSE step. Never raises."""
        try:
            research = research if isinstance(research, dict) else {}
            scraped_text = research.get("text", "") or ""
            if not isinstance(scraped_text, str):
                scraped_text = str(scraped_text)
            source_url = research.get("source_url", "") or ""
            src_kind   = research.get("source", "none")
            reachable  = research.get("reachable", False)

            body_for_len = clean_text if clean_text is not None else scraped_text
            if not isinstance(body_for_len, str):
                body_for_len = str(body_for_len)

            self._emit("")
            self._rule()
            self._emit("WEBSITE SCRAPE (RAG ingestion)")
            self._emit(f"scraped by : {resolve_source(source) if source else 'app/scraper/web_scraper.py'}")
            self._rule()

            self._emit(">>> SCRAPE SUMMARY:")
            self._emit(_dumps({
                "company":       self.company_name,
                "website_url":   source_url,
                "source":        src_kind,
                "reachable":     reachable,
                "scraped_chars": len(scraped_text),
                "merged_chars":  len(body_for_len),
                "chunk_count":   chunk_count if chunk_count is not None else "(pending)",
                "blob_path":     blob_path or "-",
                "will_embed":    bool(body_for_len.strip()),
            }))

            self._emit(">>> SCRAPED TEXT (raw" +
                       (f", first {max_chars} of {len(scraped_text)} chars"
                        if len(scraped_text) > max_chars else f", {len(scraped_text)} chars") + "):")
            if scraped_text:
                self._emit(scraped_text[:max_chars] +
                           ("\n… [truncated]" if len(scraped_text) > max_chars else ""))
            else:
                self._emit("∅ (no text scraped — site unreachable or empty)")

            if clean_text is not None and clean_text != scraped_text:
                ct = clean_text if isinstance(clean_text, str) else str(clean_text)
                self._emit(">>> MERGED TEXT TO EMBED (scrape + enrichment, first "
                           f"{max_chars} of {len(ct)} chars):")
                self._emit(ct[:max_chars] + ("\n… [truncated]" if len(ct) > max_chars else ""))

            if started:
                self._emit(f">>> duration: {_duration(started):.2f}s")
        except Exception as e:
            self._emit(f"[scrape log error: {e}]")

    def note(self, message) -> None:
        self._emit(str(message))

    def error(self, message, exc: Exception | None = None) -> None:
        try:
            self._emit("")
            self._rule("!")
            self._emit(f"ERROR: {message}")
            if exc:
                try:
                    self._emit(traceback.format_exc())
                except Exception:
                    self._emit(f"(exc: {exc})")
            self._rule("!")
        except Exception:
            pass

    def close(self, score_result=None) -> None:
        try:
            total = _duration(self._t0)
            self._emit("")
            self._rule("=")
            if score_result is not None:
                self._emit(">>> FINAL SCORE (json):")
                self._emit(f"scored by : {resolve_source(score_result)}")
                self._emit(_dumps(score_result))
            self._emit(f">>> TOTAL PIPELINE DURATION: {total:.2f}s")
            self._emit(f">>> LOG FILE: {self.path}")
            self._rule("=")
        except Exception as e:
            self._emit(f"[close error: {e}]")


# ── Duration helper (timezone-aware safe) ─────────────────────────────────────
def _duration(started) -> float:
    """
    Seconds elapsed since `started`. Works whether `started` is UTC or IST
    (Python subtracts tz-aware datetimes correctly). Never raises.
    """
    try:
        now = datetime.now(started.tzinfo) if getattr(started, "tzinfo", None) else datetime.now(IST)
        return (now - started).total_seconds()
    except Exception:
        return 0.0
