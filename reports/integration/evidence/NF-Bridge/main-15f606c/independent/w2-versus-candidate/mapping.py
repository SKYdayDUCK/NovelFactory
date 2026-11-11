"""Record exact code identity for the measured path without relying on branch names."""
from pathlib import Path
from hashlib import sha256
import ast
import json
import subprocess
import sys

out = Path(sys.argv[1])
git = lambda *args: subprocess.check_output(["git", *args])
arms = [git("rev-parse", rev).decode().strip() for rev in ("f2c584e", "695668b")]
files = [".github/workflows/p3-scale-experiments.yml", "benchmarks/p3_open_arrival.py","benchmarks/p2_behavioral_load.py","benchmarks/process_server.py","benchmarks/http_load.py","src/novel_factory/interfaces/http.py","src/novel_factory/interfaces/admission.py","src/novel_factory/application/queries.py","src/novel_factory/application/semantic_dependencies.py","src/novel_factory/infrastructure/repository.py","src/novel_factory/infrastructure/objects.py","src/novel_factory/contracts/registry.py","uv.lock"]
def names(src):
    return {f"{cls.name}.{node.name}":sha256(ast.dump(node,include_attributes=False).encode()).hexdigest()
            for cls in ast.parse(src).body if isinstance(cls,ast.ClassDef)
            for node in cls.body if isinstance(node,ast.FunctionDef)}
mapping={"ordered_shas":arms,"file_sha256":{},"class_method_source_diffs":{}}
for path in files:
    mapping["file_sha256"][path]=[sha256(git("show",s+":"+path)).hexdigest() for s in arms]
for p in ["src/novel_factory/application/kernel.py","src/novel_factory/orchestration/tasks.py","src/novel_factory/infrastructure/database.py"]:
    a,b=[names(git('show',s+':'+p)) for s in arms]
    mapping['class_method_source_diffs'][p]=[m for m in sorted(a.keys()|b.keys()) if a.get(m)!=b.get(m)]
mapping['source_observations']=[
  "The benchmark only submits /api/v1/queries/timeline requests with query.kind=STATE; each targets one subject and requires both owner and active_holder state items on the fixed WorldRef. It records offered header starts with HTTPX and verifies every response after the network window.",
  "Kernel.create_task and TaskService.create are refactored for transactional delegation and are used during make_population before services and the timed network window start. Loader tables and migrations differ between the two released schemas.",
  "Authenticated read operation, admission queues, fixture generator, validation, two-process HTTP server, timeline/STATE query implementation, repository and object store read code are byte-for-byte unchanged.",
  "A dispatched model attempt requires reserve_call/mark_call_sent. The read overload uses neither. The ordinary HTTP query may still register its pre-existing query/semantic observations; absence of NF delegation access is distinct from absence of all PostgreSQL writes."
]
out.write_text(json.dumps(mapping,indent=2)+'\n')
