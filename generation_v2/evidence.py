"""Deterministic grounding checks. Valid citations are not semantic proof."""
import hashlib
import json

from .sources import SourceError
from .store import encoded


def parse_object(text):
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n',1)[1].rsplit('```',1)[0].strip()
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise SourceError('duplicate JSON key')
            result[key]=value
        return result
    obj=json.loads(text,object_pairs_hook=unique,
                   parse_constant=lambda _: (_ for _ in ()).throw(SourceError('nonfinite JSON')))
    if not isinstance(obj,dict):
        raise SourceError('object required')
    return obj


def validate_extraction(text, batch, shard, max_diaries=3):
    obj=parse_object(text)
    if obj.get('source_revision')!=batch.revision:
        raise SourceError('source revision mismatch')
    turns={t.id:t for t in batch.turns}
    owned={s.key:s for s in shard.owned}
    facts=obj.get('facts')
    coverage=obj.get('coverage')
    if not isinstance(facts,list) or len(facts)>256 or not isinstance(coverage,list):
        raise SourceError('bounded facts and coverage required')
    ids=set()
    normalized=[]
    for fact in facts:
        fid=fact.get('id')
        if not isinstance(fid,str) or not fid or fid in ids:
            raise SourceError('unique fact id required')
        ids.add(fid)
        kind=fact.get('kind')
        if kind not in {'event','promise','relationship','emotion','detail','conflict'}:
            raise SourceError('unknown evidence kind')
        basis=fact.get('basis')
        if basis not in {'explicit','behavior','inference'}:
            raise SourceError('evidence basis required')
        claim=fact.get('claim')
        if not isinstance(claim,str) or not claim.strip() or len(claim)>1500:
            raise SourceError('bounded paraphrase required')
        citations=fact.get('citations')
        if not isinstance(citations,list) or not 1<=len(citations)<=12:
            raise SourceError('citations required')
        clean=[]
        for c in citations:
            tid,start,end=c.get('turn_id'),c.get('start'),c.get('end')
            if any(type(v) is not int for v in (tid,start,end)) or tid not in turns:
                raise SourceError('invalid reference identity')
            turn=turns[tid]
            if not any(s.turn_id==tid and s.start<=start<end<=s.end for s in shard.owned):
                raise SourceError('reference is not owned by this shard')
            if c.get('quote')!=turn.content[start:end]:
                raise SourceError('quote mismatch')
            clean.append({'turn_id':tid,'start':start,'end':end,'quote':c['quote'],
                          'role':turn.role,'event_ts':turn.event_ts,'timezone':turn.timezone})
        evidence_id=hashlib.sha256(encoded([batch.revision,clean,kind,claim,basis]).encode()).hexdigest()
        normalized.append({'id':fid,'evidence_id':evidence_id,'kind':kind,'basis':basis,
                           'claim':claim,'citations':clean,'certainty':'hypothesis' if basis=='inference' else 'source_supported',
                           'semantic_verified':False})
    seen=set()
    linked=set()
    for entry in coverage:
        key=entry.get('span_id')
        if key not in owned or key in seen:
            raise SourceError('coverage duplicated or foreign')
        seen.add(key)
        if entry.get('status') not in {'extracted','omitted'}:
            raise SourceError('pending or failed coverage cannot complete extraction')
        links=entry.get('fact_ids',[])
        if not isinstance(links,list) or any(v not in ids for v in links):
            raise SourceError('coverage links invalid')
        if entry['status']=='omitted':
            if links or not isinstance(entry.get('reason'),str) or not entry['reason'].strip():
                raise SourceError('omission requires reason and no extracted facts')
        else:
            if not links:
                raise SourceError('extracted span requires facts')
            linked.update(links)
            span=owned[key]
            for fid in links:
                fact=next(f for f in normalized if f['id']==fid)
                if not any(c['turn_id']==span.turn_id and span.start<=c['start']<c['end']<=span.end for c in fact['citations']):
                    raise SourceError('coverage link points outside span')
    if seen!=set(owned):
        raise SourceError('unaccounted source spans')
    if linked!=ids:
        raise SourceError('facts missing source coverage links')
    groups=obj.get('narratives',[])
    validate_plan(groups,normalized, max_diaries=max_diaries)
    return {'facts':normalized,'coverage':coverage,'narratives':groups,'source_revision':batch.revision}


def validate_plan(groups, facts, max_diaries=3):
    if not isinstance(groups,list) or not 0<=len(groups)<=max_diaries:
        raise SourceError('narrative limit exceeded')
    ids={f['id'] for f in facts}
    assigned=set()
    for group in groups:
        if any(not isinstance(group.get(key),str) or not group[key].strip() or len(group[key])>2000
               for key in ('theme','rationale')):
            raise SourceError('narrative theme and rationale required')
        links=group.get('fact_ids')
        if not isinstance(links,list) or not links or len(set(links))!=len(links) or set(links)-ids or assigned.intersection(links):
            raise SourceError('narrative references invalid')
        assigned.update(links)
    # All facts remain in the evidence ledger; selection for prose is explicit.
    return {'selected':sorted(assigned), 'evidence_only':sorted(ids-assigned)}
