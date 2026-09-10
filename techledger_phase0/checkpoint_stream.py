from __future__ import annotations

import csv, hashlib, json, shutil, sys, traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.webscrape import github_commit_scraper as gcs

RQ1A=Path('dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv')
RQ1B=Path('dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv')
META=Path('filtered_full_sample_repos.csv')
OUTPUT=Path('techledger_phase0/checkpoint_summary.json')
WORK=Path('/tmp/techledger_exact180_micro')
CUTOFF=datetime(2025,12,2,23,59,59,tzinfo=timezone.utc)
HORIZON=180.0
MICRO_ASSETS=5
MAX_CANDIDATES=12


def pdt(v):
    if not v: return None
    try: x=datetime.fromisoformat(str(v).replace('Z','+00:00'))
    except ValueError: return None
    if x.tzinfo is None: x=x.replace(tzinfo=timezone.utc)
    return x.astimezone(timezone.utc)

def i(v):
    try:return int(float(v or 0))
    except:return 0

def frac(a,b): return a/b if b else None

def load():
    assets=defaultdict(dict); labels={}; conflicts=0
    with RQ1A.open(newline='',encoding='utf-8') as f:
        for r in csv.DictReader(f):
            repo=(r.get('repo') or r.get('repo_key') or '').strip(); sha=(r.get('origin_commit_sha') or '').strip()
            if not repo or not sha: continue
            klass=(r.get('origin_maintenance_class') or '').strip()
            if klass:
                k=(repo,sha)
                if k in labels and labels[k]!=klass: conflicts+=1
                else: labels[k]=klass
            if (r.get('origin_primary_operational_intent') or '').strip()!='feature': continue
            n=i(r.get('tracked_line_count')); d=pdt(r.get('origin_commit_date'))
            if n<20 or d is None or d>CUTOFF: continue
            assets[repo][sha]={'origin_date':d,'tracked':n,'role':(r.get('origin_role') or '').strip()}
    with RQ1B.open(newline='',encoding='utf-8') as f:
        for r in csv.DictReader(f):
            repo=(r.get('repo') or r.get('repo_key') or '').strip(); sha=(r.get('first_terminal_commit_sha') or '').strip(); klass=(r.get('first_terminal_maintenance_class') or '').strip()
            if not repo or not sha or sha=='mixed' or not klass or klass=='mixed': continue
            k=(repo,sha)
            if k in labels and labels[k]!=klass: conflicts+=1
            else: labels[k]=klass
    return assets,labels,conflicts

def candidate_repos(assets):
    meta={}
    with META.open(newline='',encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r.get('repo_slug'):meta[r['repo_slug'].strip()]=r
    xs=[]
    for repo,a in assets.items():
        if len(a)<20:continue
        m=meta.get(repo,{})
        xs.append({'repo':repo,'feature_assets':len(a),'language':m.get('primary_language',''),'prs':i(m.get('num_PRs'))})
    xs.sort(key=lambda x:(abs(x['feature_assets']-50),-(x['prs'] or 0),x['repo']))
    return xs[:MAX_CANDIDATES]

def measure(repo, selected, line_path, labels):
    raw=Counter(); terminal=Counter(); corrective=Counter(); lab=Counter(); unlab=Counter()
    with line_path.open(encoding='utf-8') as f:
        for line in f:
            r=json.loads(line); sha=r.get('origin_commit_sha')
            if sha not in selected:continue
            raw[sha]+=1
            td=pdt(r.get('terminal_commit_date'))
            if td is None:continue
            age=(td-selected[sha]['origin_date']).total_seconds()/86400
            if not 0<=age<=HORIZON:continue
            terminal[sha]+=1
            tsha=(r.get('terminal_commit_sha') or '').strip(); klass=labels.get((repo,tsha))
            if klass:
                lab[sha]+=1
                if klass=='corrective':corrective[sha]+=1
            else:unlab[sha]+=1
    rows=[]
    for sha,a in selected.items(): rows.append({'sha':sha,'packaged':a['tracked'],'reconstructed':raw[sha],'corrective_180':corrective[sha],'terminal_180':terminal[sha],'role':a['role']})
    return {
      'assets':rows,
      'exact_count_matches':sum(r['packaged']==r['reconstructed'] for r in rows),
      'exact_count_match_share':frac(sum(r['packaged']==r['reconstructed'] for r in rows),len(rows)),
      'packaged_lines':sum(r['packaged'] for r in rows),'reconstructed_lines':sum(r['reconstructed'] for r in rows),
      'reconstructed_to_packaged_ratio':frac(sum(r['reconstructed'] for r in rows),sum(r['packaged'] for r in rows)),
      'terminal_lines_180':sum(terminal.values()),'labeled_terminal_lines_180':sum(lab.values()),'unlabeled_terminal_lines_180':sum(unlab.values()),
      'terminal_label_coverage':frac(sum(lab.values()),sum(lab.values())+sum(unlab.values())),
      'corrective_lines_180':sum(corrective.values())
    }

def attempt(info, all_assets, labels):
    repo=info['repo']; rd=gcs.ensure_local_repo(repo,WORK/'repos',full_clone=False)
    try:
        branch=gcs.run_git(rd,['rev-parse','--abbrev-ref','HEAD']).strip()
        reachable=set(gcs.run_git(rd,['rev-list','HEAD']).splitlines())
        candidates=set(all_assets[repo]) & reachable
        if len(candidates)<MICRO_ASSETS:
            return None,{'repo':repo,'reason':'fewer than five study-era feature assets reachable on current default branch','reachable':len(candidates),'branch':branch}
        chosen=sorted(candidates,key=lambda s:(all_assets[repo][s]['tracked'],all_assets[repo][s]['origin_date'],s))[:MICRO_ASSETS]
        selected={s:all_assets[repo][s] for s in chosen}
        earliest=min(a['origin_date'] for a in selected.values()); latest=max(a['origin_date'] for a in selected.values()); end=latest+timedelta(days=HORIZON,seconds=1)
        commits,_=gcs.fetch_branch_commits(repo,rd,branch,since=earliest-timedelta(days=2))
        obs=[c for c in commits if gcs.commit_datetime(c)<=end]
        bysha0={c['sha']:c for c in obs}
        if any(s not in bysha0 for s in chosen): return None,{'repo':repo,'reason':'chosen commits missing after dated branch enumeration','branch':branch}
        detailed=gcs.add_git_file_changes(repo,rd,obs); bysha={c['sha']:c for c in detailed}; out=WORK/'output'/repo.replace('/','__')
        gcs.write_outputs([bysha[s] for s in chosen],out,rd,blame_workers=2,branch=branch,branch_end_sha=obs[-1]['sha'],blame_since=None,lifecycle_commits=detailed,lifecycle_origin_shas=set(chosen))
        r=measure(repo,selected,out/'line_lifecycle.jsonl',labels); r.update({'repo':repo,'branch':branch,'selected_origin_dates':[selected[s]['origin_date'].isoformat() for s in chosen]})
        return r,None
    finally:gcs.remove_cloned_repo_dir(rd)

def main():
    if WORK.exists():shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    assets,labels,conflicts=load(); pool=candidate_repos(assets); skipped=[]; failure=None; result=None
    for info in pool:
        try:
            r,s=attempt(info,assets,labels)
            if s:skipped.append(s);continue
            result=r;break
        except Exception as e:
            failure={'repo':info['repo'],'error':str(e)[:2000],'traceback':traceback.format_exc()[-8000:]};break
    passed=bool(result and (result['exact_count_match_share'] or 0)>=.8 and .9<=(result['reconstructed_to_packaged_ratio'] or 0)<=1.1 and (result['terminal_label_coverage'] is None or result['terminal_label_coverage']>=.9))
    summary={'purpose':'Fast micro-validation of exact 180-day per-asset reconstruction before any broader rerun.','horizon_days':HORIZON,'micro_assets':MICRO_ASSETS,'candidate_pool':pool,'history_skips':skipped,'failure':failure,'result':result,'micro_pilot_pass':passed,'acceptance':'At least 4/5 exact tracked-line matches; aggregate line ratio 0.9-1.1; >=90% terminal-label coverage when terminal events exist.','guardrail':'No marker inference from this micro-pilot. If it passes, next checkpoint expands validation before full analysis.','paper_day180_anchor':{'AI_corrective_CIF':0.0461432462520287,'Human_corrective_CIF':0.032865038256452254,'ratio':1.4040222893387198},'commit_label_map_size':len(labels),'label_conflicts':conflicts}
    OUTPUT.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n',encoding='utf-8');print(json.dumps(summary,indent=2,sort_keys=True))
    if failure:raise SystemExit(2)

if __name__=='__main__':main()
