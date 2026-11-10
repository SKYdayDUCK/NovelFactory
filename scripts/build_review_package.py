"""Package a complete review tree, original evidence and Git history.

This command does not run or infer tests and does not require their success.
It keeps failed evidence and records the actual working-tree state. Existing
delivery directories are never overwritten. Run from the project root.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

EXCLUDED={".git",".venv","__pycache__",".pytest_cache",".ruff_cache",".mypy_cache",".hypothesis",
          "node_modules","build","dist","var",".runtime"}
EXCLUDED_FILES={".env",".coverage",".report.json"}

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda:source.read(1024*1024),b""):h.update(block)
    return h.hexdigest()

def copy_tree(source,destination):
    destination.mkdir(parents=True)
    for directory,folders,files in os.walk(source):
        folders[:]=sorted(d for d in folders if d not in EXCLUDED)
        for name in sorted(files):
            path=Path(directory)/name
            if name in EXCLUDED_FILES or path.suffix in {".pyc",".pyo"}:continue
            if path.is_symlink():raise RuntimeError(f"Review package requires materialized files: {path.relative_to(source)}")
            target=destination/path.relative_to(source)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)

def git(project,*args):
    return subprocess.run(["git",*args],cwd=project,check=True,text=True,capture_output=True).stdout

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir",type=Path,default=Path("../evidence"))
    parser.add_argument("--output-dir",type=Path,default=Path("../output"))
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[1]
    evidence=args.evidence_dir.resolve();output=args.output_dir.resolve()
    if not evidence.is_dir():raise RuntimeError("Original evidence directory is required")
    output.mkdir(parents=True,exist_ok=True)
    delivery=output/"novel_factory_P0_P1_review"
    archive=output/"novel_factory_P0_P1_development_review.zip"
    if delivery.exists() or archive.exists():raise RuntimeError("Choose a new output directory; existing review artifacts are preserved")
    delivery.mkdir()
    copy_tree(project,delivery/"project")
    copy_tree(evidence,delivery/"evidence")
    history=delivery/"git_history";history.mkdir()
    (history/"commits.txt").write_text(git(project,"log","--date=iso-strict","--format=fuller","--decorate","--stat"))
    (history/"tags.txt").write_text(git(project,"show-ref","--tags"))
    (history/"working_tree_status.txt").write_text(git(project,"status","--porcelain=v1"))
    (history/"change_summary.txt").write_text(git(project,"diff","--stat","design-v2.0-R1","HEAD"))
    subprocess.run(["git","bundle","create",str(history/"development-history.bundle"),"--all"],cwd=project,check=True)
    metadata={"package":"novel_factory_P0_P1_development_review","created_at_utc":datetime.now(timezone.utc).isoformat(),
              "git_head":git(project,"rev-parse","HEAD").strip(),"phases":["P0","P1"],"p2_started":False,
              "excluded_generated_directories":sorted(EXCLUDED),"original_failures_retained":True,
              "test_result_source":"evidence/integration/release_verified_results.json",
              "reports_directory":"project/reports","manifest":"MANIFEST.sha256"}
    (delivery/"DELIVERY.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+"\n")
    (delivery/"README_REVIEW.md").write_text('''# 小说 Agent 工厂 P0/P1 独立审核交付

本包包含完整可重建项目、设计基线、测试、迁移、锁文件、开发报告、原始成功/失败证据及 Git 历史。
实现范围止于 P0/P1，未进入 P2。

## 审核入口

1. 先执行 `sha256sum -c MANIFEST.sha256`，核验所有交付文件。
2. 阅读 [集成审计报告](project/reports/P0_P1_集成审计报告.md) 和 [架构差异与未完成问题](project/reports/架构差异与未完成问题.md)。
3. 按 [复现与运行指南](project/reports/复现与运行指南.md) 安装，运行原契约、真实 PostgreSQL、并发、故障和隔离恢复测试。
4. 机器结果位于 `evidence/integration/release_verified_results.json`，相邻 `.log`/`.json`/`.xml` 保存命令、状态、原始输出及 JUnit。
5. 较早的失败日志完整保留；以报告中的修复映射区分旧失败和未完成项，不能仅搜索日志中的 FAIL 就判断最终状态。

报告都在 `project/reports/`，与可运行项目一起维护。原始设计中的 PLANNED_NOT_EXECUTED 文字未伪改成本轮业务通过。
`project/MANIFEST.sha256` 是原设计包历史清单；本包当前权威清单是根目录 `MANIFEST.sha256`。

## Git 阶段

`git_history/development-history.bundle` 可用 `git clone` 到新目录；标签 `design-v2.0-R1`、`P0-complete`、`P1-complete` 区分原设计与开发阶段。
`working_tree_status.txt`、`commits.txt`、`change_summary.txt` 保存打包时的实际状态。
审阅 bundle 中代码以外，完整证据仍应使用本审核包中的 `evidence/`。

## 数据与凭据

不包含虚拟环境、数据库数据目录、运行中的对象库、缓存、私有 token 或生产数据。
配置中的本地示例数据库密码是公开测试值；bootstrap 会为新测试世界生成私有访问文件。
独立审核者应自行创建隔离 PostgreSQL 与对象目录，勿将集成测试指向生产数据库。
''')
    manifest=[]
    for path in sorted(p for p in delivery.rglob("*") if p.is_file()):
        manifest.append(f"{digest(path)}  {path.relative_to(delivery).as_posix()}")
    (delivery/"MANIFEST.sha256").write_text("\n".join(manifest)+"\n")
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as target:
        for path in sorted(p for p in delivery.rglob("*") if p.is_file()):
            target.write(path,Path(delivery.name)/path.relative_to(delivery))
    with zipfile.ZipFile(archive) as target:
        bad=target.testzip()
        if bad is not None:raise RuntimeError("ZIP verification failed: "+bad)
    result={"zip":str(archive),"sha256":digest(archive),"size_bytes":archive.stat().st_size,
            "manifest_files":len(manifest),"zip_entries":len(manifest)+1,"git_head":metadata["git_head"]}
    archive.with_suffix(".zip.sha256").write_text(result["sha256"]+"  "+archive.name+"\n")
    (output/"packaging_result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))

if __name__=="__main__":main()
