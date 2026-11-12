"""Build and import the two distributions from the checked-out frozen commit. Python 3.12."""
import hashlib, json, os, subprocess, sys, tarfile, tempfile, zipfile
from pathlib import Path

BASE = "2768203"
NATIVE = ["novel_factory/application/harness_observations.py", "novel_factory/application/creative_metrics.py",
          "novel_factory/application/audit_log.py", "novel_factory/application/mcp_runtime.py"]
WORKSHOP = ["agent_workshop/migrations/versions/w011_harness_observations.py",
            "agent_workshop/reviews_skills.py", "agent_workshop/manager.py"]
MIGRATIONS = ["migrations/versions/0019_p57_context_sources.py",
              "migrations/versions/0020_p57_harness_observations.py"]
PROBE = r'''
import importlib,json,sys
from pathlib import Path
from hashlib import sha256
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.contracts.registry import schema_for
from alembic.script import ScriptDirectory
from agent_workshop.migrate import config
prefix=Path(sys.prefix).resolve()
modules=["novel_factory.application.harness_observations","novel_factory.application.creative_metrics",
         "novel_factory.application.audit_log","novel_factory.application.mcp_runtime",
         "agent_workshop.manager","agent_workshop.reviews_skills","agent_workshop.server","agent_workshop.migrate"]
for name in modules:
    assert Path(importlib.import_module(name).__file__).resolve().is_relative_to(prefix)
catalog=RuntimeCatalog()
assert catalog.root.resolve().is_relative_to(prefix)
roles=json.loads((catalog.root/"roles/catalog.json").read_bytes())["roles"]
skills=json.loads((catalog.root/"skills/catalog.json").read_bytes())["skills"]
snapshots=[]
for role in roles:
    for mode in role["task_modes"]:
        for skill in role["skill_names"]:
            snap=catalog.snapshot(role["role_id"],mode,skill)
            assert snap["snapshot"]["role_contract"] and snap["snapshot"]["skill_instructions"]
            snapshots.append([role["role_id"],mode,skill,snap["sha256"]])
flags=CreativeHarnessConfig.from_env()
assert not any((flags.enabled, flags.dynamic_agents, flags.adaptive_instructions, flags.auto_promote))
assert ScriptDirectory.from_config(config()).get_heads()==["w011"]
for name in ("ContextPacket","PlanCandidate","ReviewReport","ExperimentFreeze","PromotionDecision"):
    assert schema_for(name,"harness")
print(json.dumps({"isolated_imports":True,"default_switches_off":True,"workshop_head":"w011",
    "role_count":len(roles),"skill_count":len(skills),"snapshots":len(snapshots),
    "snapshot_matrix_sha256":sha256(json.dumps(snapshots,sort_keys=True,separators=(",",":")).encode()).hexdigest()}))
'''
def sha(b): return hashlib.sha256(b).hexdigest()
def main():
    if len(sys.argv)!=2: raise SystemExit("usage: python packaging.py SUITE_DIR")
    if sys.version_info[:2] != (3,12): raise SystemExit("Python 3.12 is required")
    root=Path.cwd()
    out=Path(sys.argv[1]); out.mkdir(parents=True,exist_ok=True)
    record={"status":"FAILED","python":sys.version.split()[0]}
    phase="START"
    env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK":"1","PYTHONNOUSERSITE":"1","UV_NO_PROGRESS":"1"}
    env.pop("PYTHONPATH",None);env.pop("NF_CREATIVE_HARNESS_CONFIG",None)
    def cmd(args,cwd):
        result=subprocess.run([str(x) for x in args],cwd=cwd,env=env,capture_output=True)
        if result.returncode: raise RuntimeError("subprocess")
        return result.stdout
    def git(*args):return cmd(["git",*args],root)
    try:
        phase="SOURCE"
        commit=git("rev-parse","HEAD").decode().strip(); baseline=git("rev-parse",BASE).decode().strip()
        record.update(source_commit=commit,baseline_commit=baseline)
        static=git("ls-tree","--name-only","-r",baseline,"runtime").decode().splitlines()
        assert static and not git("status","--porcelain","--","pyproject.toml","src","contracts","runtime","workshop/src","workshop/migrations","workshop/pyproject.toml","migrations")
        with tempfile.TemporaryDirectory(prefix="p57-wheel-") as temp:
            temp=Path(temp);dist=temp/"dist";dist.mkdir()
            phase="BUILD"
            for project in (root,root/"workshop"):
                cmd(["uv","build","--python",sys.executable,"--wheel","--out-dir",dist,project],temp)
            cmd(["uv","build","--python",sys.executable,"--sdist","--out-dir",dist,root],temp)
            nf,aw,sd=[next(dist.glob(p)) for p in ("novel_factory-*.whl","agent_workshop-*.whl","novel_factory-*.tar.gz")]
            phase="WHEEL_MEDIA"
            with zipfile.ZipFile(nf) as n,zipfile.ZipFile(aw) as a:
                for member in NATIVE:
                    assert n.read(member)==git("show",commit+":src/"+member)
                for member in WORKSHOP:
                    path=("workshop/"+member.removeprefix("agent_workshop/") if "/migrations/" in member else "workshop/src/"+member)
                    assert a.read(member)==git("show",commit+":"+path)
                for path in static:
                    assert n.read("novel_factory/data/"+path)==git("show",baseline+":"+path)
                assert not any("migrations/versions/" in name for name in n.namelist())
                record["matching_static_files"]=len(static)
                record["matching_nf_modules"]=len(NATIVE);record["matching_workshop_files"]=len(WORKSHOP)
            phase="SOURCE_MEDIA"
            with tarfile.open(sd) as tf:
                names={m.name:m for m in tf.getmembers() if m.isfile()}
                for path in MIGRATIONS:
                    member,=[m for n,m in names.items() if n.endswith("/"+path)]
                    assert tf.extractfile(member).read()==git("show",commit+":"+path)
            record["verified_source_migrations"]=len(MIGRATIONS)
            phase="FRESH_INSTALL"
            delivery=temp/"delivery"
            cmd(["uv","venv","--python",sys.executable,delivery],temp)
            py=delivery/"bin/python"
            cmd(["uv","pip","install","--python",py,nf,aw],temp)
            cmd(["uv","pip","check","--python",py],temp)
            record["installed"]=json.loads(cmd([py,"-I","-c",PROBE],temp))
            record["distributions"]=[{"filename":p.name,"sha256":sha(p.read_bytes()),"bytes":p.stat().st_size} for p in (nf,aw,sd)]
            record["status"]="PASS";record["dependencies_consistent"]=True
    except Exception:
        record["failed_phase"]=phase
    result=out/"packaging-evidence.json"
    result.write_text(json.dumps(record,sort_keys=True,indent=2)+"\n")
    print("packaging_receipt",record["status"],sha(result.read_bytes()))
    return int(record["status"]!="PASS")
if __name__=="__main__": raise SystemExit(main())
