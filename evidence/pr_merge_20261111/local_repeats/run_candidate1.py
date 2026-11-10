import json, subprocess, sys, time
from pathlib import Path
out=Path('/workspace/scratch/f6d7dc8c770b/evidence-local/candidate1')
results=[]
for agents in (50,200):
    for run in range(1,3):
        for profile,hot in [('mixed','.2'),('overload','.8')]:
            name=f'{agents}-{profile}-{run}'
            command=[sys.executable,'-m','benchmarks.p2_behavioral_load','--agents',str(agents),'--profile',profile,'--hot-fraction',hot,'--arrival-rps','10','--open-loop-rps','1000','--rounds','5','--max-attempts','5','--retry-budget-seconds','30','--gate-scope','capacity','--service-mode','isolated-process','--api-processes','1','--output',str(out/(name+'.json'))]
            started=time.monotonic()
            with (out/(name+'.log')).open('w') as log:
                r=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
            row={'name':name,'exit_code':r.returncode,'seconds':time.monotonic()-started,'argv':command}
            results.append(row)
            print(json.dumps(row),flush=True)
            (out/'run_index.json').write_text(json.dumps(results,indent=2)+'\n')
