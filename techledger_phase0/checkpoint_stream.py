from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.webscrape import github_commit_scraper as gcs

RQ1A = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
RQ1B = Path("dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv")
META = Path("filtered_full_sample_repos.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
WORK = Path("/tmp/techledger_exact180")
CUTOFF = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)
HORIZON = 180.0
TARGET_REPOS = 3
MAX_CANDIDATES = 12


def dt(v):
    if not v:
        return None
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return x.replace(tzinfo=x.tzinfo or timezone.utc).astimezone(timezone.utc)


def integer(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def ratio(a, b):
    return a / b if b else None


def median(xs):
    if not xs:
        return None
    s = sorted(xs); m = len(s)//2
    return s[m] if len(s)%2 else (s[m-1]+s[m])/2


def split(repo):
    return int(hashlib.sha256(repo.encode()).hexdigest()[:8], 16) % 10 < 3


def load_data():
    assets = defaultdict(dict)
    labels = {}
    conflicts = 0
    with RQ1A.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            repo = (r.get("repo") or r.get("repo_key") or "").strip()
            sha = (r.get("origin_commit_sha") or "").strip()
            if not repo or not sha:
                continue
            klass = (r.get("origin_maintenance_class") or "").strip()
            if klass:
                k = (repo, sha)
                if k in labels and labels[k] != klass: conflicts += 1
                else: labels[k] = klass
            if (r.get("origin_primary_operational_intent") or "").strip() != "feature":
                continue
            n = integer(r.get("tracked_line_count")); origin = dt(r.get("origin_commit_date"))
            if n < 20 or origin is None or origin > CUTOFF:
                continue
            assets[repo][sha] = {"origin_date": origin, "tracked": n, "role": (r.get("origin_role") or "").strip()}
    with RQ1B.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            repo = (r.get("repo") or r.get("repo_key") or "").strip()
            sha = (r.get("first_terminal_commit_sha") or "").strip()
            klass = (r.get("first_terminal_maintenance_class") or "").strip()
            if not repo or not sha or sha == "mixed" or not klass or klass == "mixed":
                continue
            k=(repo,sha)
            if k in labels and labels[k] != klass: conflicts += 1
            else: labels[k]=klass
    return assets, labels, conflicts


def candidates(assets):
    meta={}
    with META.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("repo_slug"): meta[r["repo_slug"].strip()]=r
    out=[]
    for repo,a in assets.items():
        if not 40 <= len(a) <= 160: continue
        m=meta.get(repo,{})
        out.append({"repo":repo,"assets":len(a),"language":m.get("primary_language",""),"prs":integer(m.get("num_PRs")),"test_split":split(repo)})
    out.sort(key=lambda x:(abs(x["assets"]-80), -(x["prs"] or 0), x["repo"]))
    return out[:MAX_CANDIDATES]


def measure(repo, aset, path, labels):
    raw=Counter(); corr=Counter(); term=Counter(); labeled=Counter(); unlabeled=Counter()
    with path.open(encoding="utf-8") as f:
        for line in f:
            r=json.loads(line); sha=r.get("origin_commit_sha")
            if sha not in aset: continue
            raw[sha]+=1
            td=dt(r.get("terminal_commit_date"))
            if td is None: continue
            age=(td-aset[sha]["origin_date"]).total_seconds()/86400
            if not 0 <= age <= HORIZON: continue
            term[sha]+=1
            tsha=(r.get("terminal_commit_sha") or "").strip(); klass=labels.get((repo,tsha))
            if klass:
                labeled[sha]+=1
                if klass=="corrective": corr[sha]+=1
            else: unlabeled[sha]+=1
    rows=[]
    for sha,a in aset.items():
        rows.append({"sha":sha,"role":a["role"],"raw":raw[sha],"pkg":a["tracked"],"corr":corr[sha]})
    usable=[r for r in rows if r["raw"]]
    total_corr=sum(r["corr"] for r in usable); total_raw=sum(r["raw"] for r in usable)
    ranked=sorted(usable,key=lambda r:r["corr"],reverse=True); k=max(1,round(len(ranked)*.1)) if ranked else 0
    by_role={}
    for role in ("AI","Human"):
        rs=[r for r in usable if r["role"]==role]; tr=sum(r["raw"] for r in rs); cr=sum(r["corr"] for r in rs)
        by_role[role]={"assets":len(rs),"tracked_lines":tr,"corrective_180_lines":cr,"line_weighted_corrective_fraction":ratio(cr,tr),"median_asset_corrective_fraction":median([r["corr"]/r["raw"] for r in rs if r["raw"]])}
    exact=sum(r["raw"]==r["pkg"] for r in rows); lab=sum(labeled.values()); unlab=sum(unlabeled.values())
    return {"target_assets":len(aset),"raw_assets_seen":len(usable),"exact_tracked_matches":exact,"exact_tracked_match_share":ratio(exact,len(aset)),"packaged_tracked_lines":sum(r["pkg"] for r in rows),"reconstructed_tracked_lines":total_raw,"reconstructed_to_packaged_ratio":ratio(total_raw,sum(r["pkg"] for r in rows)),"terminal_lines_180":sum(term.values()),"terminal_label_coverage":ratio(lab,lab+unlab),"labeled_terminal_lines_180":lab,"unlabeled_terminal_lines_180":unlab,"corrective_180_lines":total_corr,"aggregate_corrective_180_fraction":ratio(total_corr,total_raw),"zero_corrective_asset_share":ratio(sum(r["corr"]==0 for r in usable),len(usable)),"top10_asset_share_of_corrective_180_burden":ratio(sum(r["corr"] for r in ranked[:k]),total_corr),"by_origin_role":by_role}


def try_repo(info, all_assets, labels):
    repo=info["repo"]; aset=all_assets[repo]; clone_root=WORK/"repos"
    repo_dir=gcs.ensure_local_repo(repo,clone_root,full_clone=False)
    try:
        branch=gcs.run_git(repo_dir,["rev-parse","--abbrev-ref","HEAD"]).strip()
        earliest=min(x["origin_date"] for x in aset.values()); latest=max(x["origin_date"] for x in aset.values())
        since=earliest-timedelta(days=2); horizon_end=latest+timedelta(days=HORIZON,seconds=1)
        commits,_=gcs.fetch_branch_commits(repo,repo_dir,branch,since=since)
        obs=[c for c in commits if gcs.commit_datetime(c)<=horizon_end]
        available={c["sha"] for c in obs}; target=set(aset)&available
        if len(target)<20:
            return None,{"repo":repo,"reason":"study-era target commits not sufficiently reachable on current default branch","target_assets":len(aset),"reachable_target_assets":len(target),"resolved_branch":branch}
        origin=[c for c in obs if c["sha"] in target]; end_sha=obs[-1]["sha"]
        detailed=gcs.add_git_file_changes(repo,repo_dir,obs); bysha={c["sha"]:c for c in detailed}
        outdir=WORK/"output"/repo.replace("/","__")
        gcs.write_outputs([bysha[c["sha"]] for c in origin],outdir,repo_dir,blame_workers=2,branch=branch,branch_end_sha=end_sha,blame_since=None,lifecycle_commits=detailed,lifecycle_origin_shas=target)
        result=measure(repo,{s:aset[s] for s in target},outdir/"line_lifecycle.jsonl",labels)
        result.update({"resolved_branch":branch,"reachable_target_assets":len(target),"unreachable_target_assets":len(aset)-len(target)})
        return result,None
    finally:
        gcs.remove_cloned_repo_dir(repo_dir)


def main():
    if WORK.exists(): shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    assets,labels,conflicts=load_data(); pool=candidates(assets)
    results={}; skipped=[]; failures=[]
    for info in pool:
        if len(results)>=TARGET_REPOS: break
        try:
            r,s=try_repo(info,assets,labels)
            if s: skipped.append(s)
            else: results[info["repo"]]=r
        except Exception as e:
            failures.append({"repo":info["repo"],"error":str(e)[:2000],"traceback":traceback.format_exc()[-8000:]})
            break
    checks={}
    for repo,r in results.items():
        checks[repo]={"tracked_reconciliation_pass":(r["exact_tracked_match_share"] or 0)>=.95 and .95 <= (r["reconstructed_to_packaged_ratio"] or 0) <= 1.05,"terminal_label_coverage_pass":(r["terminal_label_coverage"] or 0)>=.95}
    success=len(results)==TARGET_REPOS and not failures and all(all(v.values()) for v in checks.values())
    summary={"purpose":"Validate exact 180-day per-asset corrective burden reconstruction on three repositories with preserved study-era history.","horizon_days":HORIZON,"candidate_pool":pool,"history_rewrite_skips":skipped,"failures":failures,"commit_label_map_size":len(labels),"commit_label_conflicts":conflicts,"results":results,"validation_checks":checks,"pilot_pass":success,"guardrail":"Pilot is pipeline validation only; no TechLedger marker inference from three repos.","next_if_pass":"Scale in recoverable repository batches, persist exact-180 asset outcomes, then rerun concentration and marker screens.","next_if_fail":"Stop and diagnose reconciliation or label coverage before scaling."}
    OUTPUT.write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps(summary,indent=2,sort_keys=True))
    if failures: raise SystemExit(2)

if __name__=="__main__": main()
