import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DEFAULTS = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
    "AZURE_SQL_SERVER": "example.database.windows.net",
    "AZURE_SQL_DATABASE": "qualitylead",
    "AZURE_SQL_USERNAME": "testuser",
    "AZURE_SQL_PASSWORD": "testpass",
    "AZURE_BLOB_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=testacct;AccountKey=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;EndpointSuffix=core.windows.net",
    "QDRANT_URL": "https://example.qdrant.io",
    "QDRANT_API_KEY": "test-key",
    "AUTH_SECRET_KEY": "12345678901234567890123456789012",
}

for key, value in DEFAULTS.items():
    os.environ.setdefault(key, value)
