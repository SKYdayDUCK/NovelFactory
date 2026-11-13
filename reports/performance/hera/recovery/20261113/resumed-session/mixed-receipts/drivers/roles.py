"""Compare an immutable premerge base and two HERA flag settings in opposite orders."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess


def git(path, *args):
    return subprocess.check_output(["git", "-C", path, *args], text=True).strip()


def env_for(tree, py, flag, database):
    runtime = (tree / "src", tree / "workshop/src", tree)
    env = {**os.environ, "NF_HERA_COMPACT_ROLE_READS": flag, "PYTHONPATH": os.pathsep.join(map(str,runtime))}
    env["NF_DATABASE_URL"] = env["NF_TEST_DATABASE_URL"].rsplit("/",1)[0] + "/" + database
    source = subprocess.check_output([py,"-c",
        "import inspect,novel_factory.application.mcp_runtime as m;print(inspect.getfile(m))"],
        env=env,text=True).strip()
    assert Path(source).is_relative_to(tree)
    return env


SQL = """select count(*), count(o.outbox_id), count(*) filter (where a.outcome='OK' and a.evidence_id=t.context_bundle_id),
count(*) filter(where a.outcome='ERROR') from nf_audit_events a join nf_tasks t
on t.task_id=a.task_id and t.principal_id=a.principal_id::text left join nf_outbox o
on o.dedupe_key='audit:' || a.event_id::text and o.topic='audit.recorded'
where a.tool_name in ('nf_task_get','nf_context_get') and a.event_type in ('mcp.started','mcp.finished')"""


def main():
    parser=argparse.ArgumentParser()
    for name in ["base","candidate","base-python","candidate-python","output"]:
        parser.add_argument("--"+name,type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    driver = Path(__file__).with_name("role-transport.py").resolve()
    for i, arm in enumerate(["base","default","compact","compact","default","base"]):
        tree=(args.base if arm=="base" else args.candidate).resolve()
        py=(args.base_python if arm=="base" else args.candidate_python).absolute()
        sub=f"role_{arm}_{i}"
        env=env_for(tree,py,"1" if arm=="compact" else "0",sub)
        assert not git(tree,"status","--porcelain","--untracked-files=no")
        assert not any(p.exists() for p in [args.output/f"{i}-{arm}.json"])
        subprocess.run(["createdb",sub],check=True)
        subprocess.run([py,"-m","alembic","upgrade","head"],env=env,cwd=tree,check=True,stdout=subprocess.DEVNULL)
        outfile=(args.output/f"{i}-{arm}.json").resolve()
        subprocess.run([py,driver,outfile,"--samples-per-role","64","--offered-rps","15"],env=env,cwd=tree,check=True)
        report=json.loads(outfile.read_text())
        assert report["source_commit_at_start"]==git(tree,"rev-parse","HEAD")
        counts=subprocess.check_output(["psql","-XAt","-d",sub,"-c",SQL],text=True).strip()
        audited,paired,success,error=map(int,counts.split("|"))
        assert audited==paired==2*(success+error)
        assert success==4+sum(phase["statuses"].get("OK",0) for phase in report["phases"])
        report.update(arm=arm,order=i,child_compact_flag=env["NF_HERA_COMPACT_ROLE_READS"],
                      source_tree=git(tree,"rev-parse","HEAD:src"),audit_outbox_evidence_counts=counts,
                      runner_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
                      worker_driver_affinity=sorted(os.sched_getaffinity(0)),postgres_affinity=[3])
        outfile.write_text(json.dumps(report,indent=2)+"\n")
        print(i,arm,counts,[(p["verified_rps"],p["all_replies_arrival_to_reply_ms"]) for p in report["phases"]],flush=True)
        subprocess.run(["dropdb",sub],check=True)


if __name__ == "__main__":
    main()
