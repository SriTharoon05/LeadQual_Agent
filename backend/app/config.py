from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Dict, List, Tuple


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    # ── Azure OpenAI ──────────────────────────────────────────────────────────
    AZURE_OPENAI_API_KEY: str
    AZURE_OPENAI_ENDPOINT: str
    AZURE_OPENAI_API_VERSION: str = "2025-03-01-preview"
    AZURE_OPENAI_DEPLOYMENT: str = "gpt-4.1-mini"
    LLM_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=600)

    # ── Azure SQL ─────────────────────────────────────────────────────────────
    AZURE_SQL_SERVER: str
    AZURE_SQL_DATABASE: str
    AZURE_SQL_USERNAME: str
    AZURE_SQL_PASSWORD: str
    AZURE_SQL_DRIVER: str = "ODBC Driver 17 for SQL Server"

    # ── Azure Blob Storage ────────────────────────────────────────────────────
    AZURE_BLOB_CONNECTION_STRING: str
    AZURE_BLOB_CONTAINER: str = "ailead-uploads"

    # ── Redis / Celery ────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    MAX_WORKERS: int = 10
    CELERY_IMPORT_QUEUE: str = "lead_imports"
    CELERY_EMBED_QUEUE: str = "lead_embeddings"
    CELERY_EXPORT_QUEUE: str = "lead_exports"
    EXPORT_BATCH_SIZE: int = Field(default=250, ge=50, le=1000)
    EXPORT_CACHE_SECONDS: int = Field(default=600, ge=60, le=3600)
    EXPORT_TIMEOUT_SECONDS: int = Field(default=1800, ge=120, le=7200)
    SCRAPE_MAX_HTML_BYTES: int = Field(default=2_097_152, ge=65_536, le=8_388_608)
    SCRAPE_MAX_TEXT_CHARS: int = Field(default=250_000, ge=4_000, le=1_000_000)

    # ── App ───────────────────────────────────────────────────────────────────
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    MAX_UPLOAD_SIZE_MB: int = 50
    MAX_ROWS_PER_UPLOAD: int = 100000

    # Comma-separated browser origins. Never use "*" together with credentials.
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"
    TRUSTED_HOSTS: str = "localhost,127.0.0.1"
    AUTH_SECRET_KEY: str = "change-me-in-production"
    AUTH_TOKEN_TTL_MINUTES: int = 480

        # Qdrant Cloud
    QDRANT_URL:            str
    QDRANT_API_KEY:        str
    QDRANT_COLLECTION:     str = "company_chunks"
    EMBED_DIM:             int = 1536
    EMBED_DEPLOYMENT: str = "text-embedding-ada-002"

    ENABLE_SMTP_CHECK: bool = False
    SMTP_TIMEOUT_S: float = 5.0
    DNS_TIMEOUT_S: float = 5.0

    @property
    def CHAT_DEPLOYMENT(self) -> str:
        """Reuse the existing gpt-4o deployment for the RAG chat router."""
        return self.AZURE_OPENAI_DEPLOYMENT    

    @property
    def cors_origins(self) -> list[str]:
        return [value.strip().rstrip("/") for value in self.CORS_ORIGINS.split(",") if value.strip()]

    @property
    def trusted_hosts(self) -> list[str]:
        return [value.strip() for value in self.TRUSTED_HOSTS.split(",") if value.strip()]

    @property
    def azure_sql_connection_string(self) -> str:
        return (
            f"mssql+pyodbc://{quote_plus(self.AZURE_SQL_USERNAME)}:{quote_plus(self.AZURE_SQL_PASSWORD)}"
            f"@{self.AZURE_SQL_SERVER}/{quote_plus(self.AZURE_SQL_DATABASE)}"
            f"?driver={self.AZURE_SQL_DRIVER.replace(' ', '+')}"
            f"&Encrypt=yes&TrustServerCertificate=no&Connection+Timeout=30"
        )


# ── Singleton ─────────────────────────────────────────────────────────────────
settings = Settings()

if settings.APP_ENV.lower() in {"production", "prod"} and (
        len(settings.AUTH_SECRET_KEY.encode("utf-8")) < 32
        or settings.AUTH_SECRET_KEY in {"change-me-in-production", "replace-with-at-least-32-random-bytes"}):
    raise RuntimeError("Configure AUTH_SECRET_KEY with at least 32 random bytes in production")


# ── Canonical 29 output columns (order matters — matches Excel template) ─────
CANONICAL_COLUMNS: List[str] = [
    # Contact Information (cols 1–8)
    "Contact Name",
    "Job Title",
    "Contact Department",
    "Contact Seniority",
    "LinkedIn Profile URL",
    "Email Address",
    "Phone Number",
    "Direct Phone Number",
    # Contact Location (cols 9–11)
    "Contact Country",
    "Contact State/Region",
    "Contact City",
    # Company Information (cols 12–20)
    "Company Name",
    "Company Website",
    "Company LinkedIn URL",
    "Industry",
    "Sub Industry",
    "Employee Count",
    "Annual Revenue",
    "Company Description",    # AI FILL
    "Technologies Used",      # AI FILL
    # Company Location (cols 21–25)
    "Company Country",
    "Company State/Region",
    "Company City",
    "Company HQ Location",
    "HQ Phone Number",
    # Additional Information (cols 26–29)
    "CRM Account ID",
    "CRM Contact ID",
    "Lead Source",
    "Notes",
]

# Column group headers (row 1 of the output Excel — merged cells)
COLUMN_GROUPS: Dict[str, List[str]] = {
    "Contact Information":  CANONICAL_COLUMNS[0:8],
    "Contact Location":     CANONICAL_COLUMNS[8:11],
    "Company Information":  CANONICAL_COLUMNS[11:20],
    "Company Location":     CANONICAL_COLUMNS[20:25],
    "Additional Information": CANONICAL_COLUMNS[25:29],
}

# Extra AI/validation columns appended after the 29 in the export
EXPORT_EXTRA_COLUMNS: List[str] = [
    "ICP Score",
    "Contact Score",
    "Growth Score",
    "Final Score",
    "Priority",
    "Reasoning",
    "Growth Signals",
    "Outreach Intelligence",
    "Source URLs",
    "Qualification Status",
    "Scoring Version",
]


# ── Column mapping: input column name → canonical column name ─────────────────
# Keys are lowercase-stripped for fuzzy matching (see column_mapper.py).
# Add new CRM source column names here — they map to the same canonical names.
COLUMN_MAPPING: Dict[str, str] = {

    # ── ZoomInfo exact column names ───────────────────────────────────────────
    "first name":                       "_FIRST_NAME",    # combined below
    "last name":                        "_LAST_NAME",     # combined below
    "middle name":                      "_IGNORE",
    "salutation":                       "_IGNORE",
    "suffix":                           "_IGNORE",
    "job title":                        "Job Title",
    "job title hierarchy level":        "_IGNORE",
    "management level":                 "Contact Seniority",
    "job start date":                   "_IGNORE",
    "job function":                     "Contact Department",
    "department":                       "Contact Department",
    "company division name":            "_IGNORE",
    "direct phone number":              "Direct Phone Number",
    "email address":                    "Email Address",
    "email domain":                     "_IGNORE",
    "mobile phone":                     "Phone Number",
    "highest level of education":       "_IGNORE",
    "contact accuracy score":           "_IGNORE",
    "contact accuracy grade":           "_IGNORE",
    "zoominfo contact profile url":     "_IGNORE",
    "linkedin contact profile url":     "LinkedIn Profile URL",
    "notice provided date":             "_IGNORE",
    "person street":                    "_IGNORE",
    "person city":                      "Contact City",
    "person state":                     "Contact State/Region",
    "person zip code":                  "_IGNORE",
    "country":                          "Contact Country",
    "zoominfo company id":              "CRM Account ID",
    "company name":                     "Company Name",
    "website":                          "Company Website",
    "founded year":                     "_IGNORE",
    "company hq phone":                 "HQ Phone Number",
    "fax":                              "_IGNORE",
    "ticker":                           "_IGNORE",
    "revenue (in 000s usd)":            "Annual Revenue",
    "revenue range (in usd)":           "_IGNORE",
    "est. it department budget (in 000s usd)": "_IGNORE",
    "employees":                        "Employee Count",
    "employee range":                   "_IGNORE",
    "sic code 1":                       "_IGNORE",
    "sic code 2":                       "_IGNORE",
    "sic codes":                        "_IGNORE",
    "naics code 1":                     "_IGNORE",
    "naics code 2":                     "_IGNORE",
    "naics codes":                      "_IGNORE",
    "primary industry":                 "Industry",
    "primary sub-industry":             "Sub Industry",
    "all industries":                   "_IGNORE",
    "all sub-industries":               "_IGNORE",
    "industry hierarchical category":   "_IGNORE",
    "secondary industry hierarchical category": "_IGNORE",
    "alexa rank":                       "_IGNORE",
    "zoominfo company profile url":     "_IGNORE",
    "linkedin company profile url":     "Company LinkedIn URL",
    "facebook company profile url":     "_IGNORE",
    "twitter company profile url":      "_IGNORE",
    "ownership type":                   "_IGNORE",
    "business model":                   "_IGNORE",
    "certified active company":         "_IGNORE",
    "certification date":               "_IGNORE",
    "total funding amount (in 000s usd)": "_IGNORE",
    "recent funding amount (in 000s usd)": "_IGNORE",
    "recent funding round":             "_IGNORE",
    "recent funding date":              "_IGNORE",
    "recent investors":                 "_IGNORE",
    "all investors":                    "_IGNORE",
    "company street address":           "_IGNORE",
    "company city":                     "Company City",
    "company state":                    "Company State/Region",
    "company zip code":                 "_IGNORE",
    "company country":                  "Company Country",
    "full address":                     "Company HQ Location",
    "number of locations":              "_IGNORE",
    "query name":                       "_IGNORE",
    "zoominfo contact id":              "CRM Contact ID",
        # ── InsideView / ZoomInfo extra columns ───────────────────────────────────
    "job level":                        "Contact Seniority",
    "desk phone":                       "Phone Number",
    "executive city":                   "Contact City",
    "executive state":                  "Contact State/Region",
    "executive country":                "Contact Country",
    "executive linkedin profile":       "LinkedIn Profile URL",
    "executive id":                     "CRM Contact ID",
    "insideview company id":            "CRM Account ID",
    "employment id":                    "_IGNORE",
    "revenue":                          "Annual Revenue",
    "sub-industry":                     "Sub Industry",
    "company linkedin profile":         "Company LinkedIn URL",
    "street":                           "Company HQ Location",
    "state":                            "Company State/Region",

    # ── Salesforce common field names ─────────────────────────────────────────
    "first name (contact)":             "_FIRST_NAME",
    "last name (contact)":              "_LAST_NAME",
    "full name":                        "Contact Name",
    "contact: full name":               "Contact Name",
    "title":                            "Job Title",
    "contact title":                    "Job Title",
    "email":                            "Email Address",
    "mobile":                           "Phone Number",
    "phone":                            "Direct Phone Number",
    "account name":                     "Company Name",
    "account: website":                 "Company Website",
    "lead source":                      "Lead Source",
    "contact id":                       "CRM Contact ID",
    "account id":                       "CRM Account ID",
    "billing city":                     "Company City",
    "billing state":                    "Company State/Region",
    "billing country":                  "Company Country",
    "billing street":                   "Company HQ Location",
    "mailing city":                     "Contact City",
    "mailing state":                    "Contact State/Region",
    "mailing country":                  "Contact Country",
    "industry":                         "Industry",
    "description":                      "Company Description",

    # ── HubSpot common field names ────────────────────────────────────────────
    "firstname":                        "_FIRST_NAME",
    "lastname":                         "_LAST_NAME",
    "jobtitle":                         "Job Title",
    "mobilephone":                      "Phone Number",
    "phone number":                     "Direct Phone Number",
    "city":                             "Contact City",
    "state/region":                     "Contact State/Region",
    "country/region":                   "Contact Country",
    "company":                          "Company Name",
    "company domain name":              "Company Website",
    "linkedin company page":            "Company LinkedIn URL",
    "number of employees":              "Employee Count",
    "annual revenue":                   "Annual Revenue",

    # ── Apollo.io common field names ──────────────────────────────────────────
    "person linkedin url":              "LinkedIn Profile URL",
    "person country":                   "Contact Country",
    "work direct phone":                "Direct Phone Number",
    "corporate phone":                  "HQ Phone Number",
    "organization name":                "Company Name",
    "organization linkedin url":        "Company LinkedIn URL",
    "organization website":             "Company Website",
    "organization city":                "Company City",
    "organization state":               "Company State/Region",
    "organization country":             "Company Country",
    "primary phone":                    "Phone Number",
    "seniority":                        "Contact Seniority",
    "departments":                      "Contact Department",
}

# Special tokens used by the parser
_FIRST_NAME_KEY = "_FIRST_NAME"
_LAST_NAME_KEY  = "_LAST_NAME"
_IGNORE_KEY     = "_IGNORE"


# ── Defaults ──────────────────────────────────────────────────────────────────
LEAD_SOURCE_DEFAULT: str = "ZoomInfo"


# ── Scoring weights (must sum to 1.0) ────────────────────────────────────────
from app.scoring.policy import WEIGHTS as _QUALIFICATION_WEIGHTS

# Compatibility names for callers; scoring.policy is the source of truth.
SCORING_WEIGHTS: Dict[str, float] = {
    "icp_fit":          _QUALIFICATION_WEIGHTS["icp"],
    "growth_signals":   _QUALIFICATION_WEIGHTS["growth"],
    "contact_quality":  _QUALIFICATION_WEIGHTS["contact"],
}

# ── Priority tiers (inclusive ranges) ────────────────────────────────────────
PRIORITY_TIERS: Dict[str, Tuple[int, int]] = {
    "High":   (80, 100),
    "Medium": (50, 79),
    "Low":    (0,  49),
}


# ── Supported file extensions ─────────────────────────────────────────────────
SUPPORTED_EXTENSIONS: List[str] = [".xlsx", ".xls", ".xlsm", ".csv"]


# ── Celery queue name ─────────────────────────────────────────────────────────
CELERY_QUEUE_NAME: str = "lead_processing"


# ── Web search queries template (used by web_researcher.py) ──────────────────
# {company} and {person} are replaced at runtime
WEB_SEARCH_QUERIES: Dict[str, str] = {
    "company_overview":  '"{company}" employees revenue industry site:linkedin.com OR crunchbase.com OR bloomberg.com',
    "person_verify": '"{person}" "{company}" {job_title} {linkedin}',
    "tech_stack":        '"{company}" technology software tools stack',
    "growth_signals":    '"{company}" expansion hiring funding office recent',
    "company_about":     '"{company}" about overview what they do',
}


def get_priority(score: float) -> str:
    """Return priority tier label for a given score."""
    # Lower bounds avoid gaps such as 79.1..79.9 falling through to Low.
    from app.scoring.policy import priority_for
    return priority_for(score)
