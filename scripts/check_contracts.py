"""Fail CI on schema, OpenAPI, migration-head or packaged-schema drift."""
from pathlib import Path
import json
from importlib.resources import files

from alembic.config import Config
from alembic.script import ScriptDirectory
from jsonschema import Draft202012Validator

from agentforge_server.app import app
from agentforge_server.db import SCHEMA_REVISION

ROOT = Path(__file__).resolve().parents[1]
paths = sorted((ROOT / "protocol/v1").glob("*.schema.json"))
for path in paths:
    Draft202012Validator.check_schema(json.loads(path.read_text()))
    assert files("agentforge_protocol").joinpath(path.name).read_bytes() == path.read_bytes(), path
print(f"SCHEMAS_OK: {len(paths)}; packaged resources match")
assert app.openapi() == json.loads((ROOT / "protocol/v1/openapi.json").read_text())
print(f"OPENAPI_MATCH: {len(app.openapi()['paths'])} paths")
config = Config(str(ROOT / "alembic.ini"))
config.set_main_option("script_location", str(ROOT / "migrations"))
assert ScriptDirectory.from_config(config).get_heads() == [SCHEMA_REVISION]
print(f"MIGRATION_HEAD_MATCH: {SCHEMA_REVISION}")
