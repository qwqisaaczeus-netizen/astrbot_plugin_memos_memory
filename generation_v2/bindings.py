"""Final prose owns its evidence links; the immutable narrative plan is retained."""
import copy
import hashlib
import math

from .sources import SourceError
from .store import encoded


def prose_links(draft, allowed):
    rows=draft.get('prose_links',[])
    if not isinstance(rows,list):raise SourceError('invalid prose links')
    body=''.join(str(draft.get('body','')).split())
    ids=[]
    for row in rows:
        if (not isinstance(row,dict) or row.get('fact_id') not in allowed
                or not isinstance(row.get('quote'),str) or not row['quote'].strip()
                or ''.join(row['quote'].split()) not in body):
            raise SourceError('prose link does not occur in this diary')
        if row['fact_id'] not in ids:ids.append(row['fact_id'])
    return ids


def retain_verified_prose_links(draft, allowed):
    """Invalid body quotations remain references, never confirmed prose coverage."""
    rows=draft.get('prose_links',[])
    if not isinstance(rows,list):raise SourceError('invalid prose links')
    result=copy.deepcopy(draft)
    body=''.join(str(draft.get('body','')).split())
    kept=[]
    rejected=list(result.get('prose_link_rejections',[]))
    for row in rows:
        if (not isinstance(row,dict) or row.get('fact_id') not in allowed
                or not isinstance(row.get('quote'),str) or not row['quote'].strip()):
            raise SourceError('unknown or malformed prose association')
        if ''.join(row['quote'].split()) in body:
            kept.append(row)
        else:
            rejected.append({**row,'reason':'quote_not_in_diary','disposition':'reference_only'})
    result['prose_links']=kept
    if rejected:result['prose_link_rejections']=rejected
    return result


def final_groups(plan, drafts):
    groups = plan['narratives']
    facts = {f['id']: f for f in plan['facts']}
    if len(facts) != len(plan['facts']):
        raise SourceError('duplicate batch fact identity')
    if (len(drafts) != len(groups) or any(type(d.get('index')) is not int for d in drafts)
            or {d['index'] for d in drafts} != set(range(len(groups)))):
        raise SourceError('final draft identities invalid')
    selected = set()
    by_index = {}
    prose_by_index = {}
    for draft in drafts:
        links = draft.get('fact_ids')
        if (not isinstance(links, list) or not links or any(not isinstance(f, str) for f in links)
                or len(set(links)) != len(links) or not set(links) <= facts.keys()):
            raise SourceError('final prose references unknown or duplicate evidence')
        by_index[draft['index']] = links
        if any(facts[f].get('claim_audit',{}).get('status')=='uncertain' for f in links):
            raise SourceError('uncertain claim cannot be asserted by diary binding')
        prose_by_index[draft['index']]=(prose_links(draft,set(links)) if plan.get('claim_audit') else links)
        selected.update(links)
    result = []
    for i, original in enumerate(groups):
        associated = by_index[i]
        prose = prose_by_index[i]
        residual = [f for f in original['fact_ids'] if f not in selected]
        group = copy.deepcopy(original)
        group['prose_fact_ids'] = list(prose)
        if plan.get('claim_audit'):
            group['reference_fact_ids'] = [f for f in associated if f not in prose]
        group['evidence_only_fact_ids'] = residual
        group['fact_ids'] = list(associated) + residual
        group['rebound_fact_ids'] = [f for f in associated if f not in original['fact_ids']]
        times = [c['event_ts'] for fid in associated for c in facts[fid]['citations']
                 if c.get('event_ts') and math.isfinite(c['event_ts']) and c['event_ts'] > 0]
        group['event_start'] = min(times) if times else None
        group['event_end'] = max(times) if times else None
        group['time_basis'] = 'source_recording_time' if times else 'unknown'
        result.append(group)
    return result


def item_fact_ids(item, plan):
    # Old, already published manifests retain their original interpretation.
    return item.get('fact_ids', plan['narratives'][item['ordinal']]['fact_ids'])


def verify_plan_evidence(plan, batch):
    if plan['source_revision'] != batch.revision or plan['batch_id'] != batch.batch_id:
        raise SourceError('final evidence source changed')
    turns = {t.id: t for t in batch.turns}
    for fact in plan['facts']:
        if not fact.get('citations'):
            raise SourceError('fact has no original evidence')
        for c in fact['citations']:
            turn = turns.get(c.get('turn_id'))
            start, end = c.get('start'), c.get('end')
            if (turn is None or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(turn.content)
                    or turn.content[start:end] != c.get('quote') or turn.role != c.get('role')
                    or turn.event_ts != c.get('event_ts') or turn.timezone != c.get('timezone')):
                raise SourceError('final evidence is not grounded in this batch')
        digest=hashlib.sha256(encoded([batch.revision,fact['citations'],fact['kind'],fact['claim'],fact['basis']]).encode()).hexdigest()
        if fact.get('evidence_id')!=digest:
            raise SourceError('fact identity changed after extraction')
