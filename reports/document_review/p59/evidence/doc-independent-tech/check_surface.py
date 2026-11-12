"""Independent cross-check of runtime registrations, reference headings and point links."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from uuid import UUID

from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator, FormatChecker
from novel_factory.application.delegation import SCHEMAS
from novel_factory.contracts.registry import _REGISTRY
from novel_factory.interfaces.http import create_app
from novel_factory.interfaces.mcp_stdio import _TOOLS
from agent_workshop.server import TOOLS

root = Path.cwd()
catalog = json.loads((root/"docs/reference/catalog.json").read_text())
examples = json.loads((root/"docs/reference/examples.json").read_text())
fake_pool = SimpleNamespace(size=lambda: 8)
kernel = SimpleNamespace(db=SimpleNamespace(engine=SimpleNamespace(pool=fake_pool)))
server = create_app(kernel, query_service=object())
tags = {key: family for family, group in [("nf-mcp", _TOOLS), ("workshop-mcp", TOOLS), ("installation-mcp", SCHEMAS)] for key in group}
for route in server.routes:
    if isinstance(route, APIRoute):
        for method in route.methods:
            tags[method+" "+route.path] = "http"
assert set(tags) == set(catalog) == set(examples)
for key, tag in tags.items():
    assert catalog[key]["transport"] == tag
    row = examples[key]
    schema = catalog[key].get("inputSchema", {"type":"object","maxProperties":0})
    Draft202012Validator(schema, registry=_REGISTRY, format_checker=FormatChecker()).validate(row["request"])
    if "{" in key:
        for field in re.findall(r"\{([a-z_]+)\}",key):
            uuid=(row.get("pathBindings") or {}).get(field)
            if uuid:
                assert str(UUID(uuid)) == uuid
checks = {}
for language in ["zh-CN","en"]:
    path=next((root/"docs"/language).glob("07-*.md"))
    raw=path.read_text()
    sections=re.split(r"(?m)^### `([^`]+)`\s*$",raw)
    assert sections[1::2] and len(sections[1::2])==len(set(sections[1::2])) and set(sections[1::2])==set(tags)
    links,errors=0,[]
    for name, section in zip(sections[1::2],sections[2::2]):
        for filename, line_no in re.findall(r"\]\((\.\./reference/(?:catalog|examples)\.json)#L([0-9]+)\)", section):
            line=(path.parent/filename).read_text().splitlines()[int(line_no)-1]
            if not(line.strip().startswith(json.dumps(name)+":")):
                errors.append((name,filename,int(line_no),line[:80]))
            links+=1
    checks[language]={"interfaces":len(sections[1::2]),"point_links":links,"bad_point_links":errors}
result={"registries":dict(Counter(tags.values())), "all_input_schemas_valid":True, "examples":dict(Counter(e["kind"] for e in examples.values())),
        "path_binding_examples":sum(bool(v.get("pathBindings")) for v in examples.values()),"reference_docs":checks,
        "public_tree_sha256":{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted([root/"README.md", root/"README.en.md",*root.glob("docs/en/*.md"),*root.glob("docs/zh-CN/*.md"),*root.glob("docs/reference/*.json"),root/"scripts/export_public_api.py"])}}
Path(sys.argv[1]).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
print({k: v for k,v in result.items() if k not in {"reference_docs","public_tree_sha256"}})
print([(lang,x["point_links"],len(x["bad_point_links"])) for lang,x in checks.items()])
