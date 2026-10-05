"""One bounded supplemental extraction; never treat missing turns as omitted facts."""
from .evidence import parse_object
from .quote_grounding import normalize_fact_ids, resolve_quotes
from .sources import Shard, SourceError
from .store import encoded

PROMPT='''Extract evidence only from missing_owned spans. Return the extraction JSON
{facts:[{id,kind,claim,basis,citations:[{span_id,quote}]}],omissions:[{span_id,reason}]}.
Use short exact source quotations, Chinese claims for Chinese sources and unique STRING
IDs not in reserved_fact_ids. kind: event,promise,relationship,emotion,detail,conflict.
basis: explicit,behavior,inference. Preserve the user's intent, emotion and requests.
Every supplied span needs grounded evidence or a substantive omission reason. Do not
invent facts, refer to other spans, write diary prose or change existing facts.'''


def request(text,batch,shard,max_diaries=3):
    try:
        obj,_=normalize_fact_ids(parse_object(text))
        linked={c['span_id'] for f in obj['facts'] for c in f['citations']}
        omitted={entry['span_id'] for entry in obj.get('omissions',[])}
        missing=[span for span in shard.owned if span.key not in linked|omitted and span.end>span.start]
        if not 1<=len(missing)<=8:return None
        # Validate existing evidence independently; these provisional omissions are
        # never persisted or delivered and must be replaced by the supplement.
        provisional={**obj,'omissions':obj.get('omissions',[])+[
            {'span_id':s.key,'reason':'pending supplemental extraction, validation only'} for s in missing]}
        resolve_quotes(encoded(provisional),batch,shard,max_diaries)
        turns={t.id:t for t in batch.turns}
        payload={'missing_owned':[{'span_id':s.key,'role':turns[s.turn_id].role,
                    'event_ts':turns[s.turn_id].event_ts,'text':turns[s.turn_id].content[s.start:s.end]}
                    for s in missing], 'reserved_fact_ids':[f['id'] for f in obj['facts']]}
        if len(encoded(payload))>16000:return None
        return payload
    except (ValueError,TypeError,KeyError,AttributeError):return None


def apply(text,supplement_text,payload,batch,shard,max_diaries=3):
    if request(text,batch,shard,max_diaries)!=payload:
        raise SourceError('coverage repair input changed')
    original,identity_repairs=normalize_fact_ids(parse_object(text))
    supplement,supplement_ids=normalize_fact_ids(parse_object(supplement_text))
    allowed={entry['span_id'] for entry in payload['missing_owned']}
    subset=Shard(tuple(s for s in shard.owned if s.key in allowed),())
    resolve_quotes(encoded(supplement),batch,subset,max_diaries)
    if {f['id'] for f in original['facts']} & {f['id'] for f in supplement['facts']}:
        raise SourceError('supplement cannot overwrite existing evidence')
    merged={**original,'facts':original['facts']+supplement['facts'],
            'omissions':original.get('omissions',[])+supplement.get('omissions',[])}
    result=resolve_quotes(encoded(merged),batch,shard,max_diaries)
    result['compatibility_repairs']=identity_repairs+supplement_ids+result['compatibility_repairs']
    result['coverage_repairs']={'version':'coverage-repair-v1','span_ids':sorted(allowed),
                                'added_fact_ids':[f['id'] for f in supplement['facts']]}
    return result
