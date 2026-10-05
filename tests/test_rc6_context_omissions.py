import hashlib
import json
import unittest

from astrbot_plugin_memos_memory.generation_v2.quote_grounding import resolve_quotes
from astrbot_plugin_memos_memory.generation_v2.sources import SourceBatch, Turn, Span, Shard, SourceError
from astrbot_plugin_memos_memory.generation_v2.citation_repair import request, apply
from astrbot_plugin_memos_memory.generation_v2.quote_compat import folded, resolve


class ContextOmissionTests(unittest.TestCase):
    def setUp(self):
        contents=['前文参考', '我喝了水', '后文参考']
        turns=tuple(Turn(i+1,i,'assistant',text,1,'Asia/Shanghai',hashlib.sha256(text.encode()).hexdigest())
                    for i,text in enumerate(contents))
        self.batch=SourceBatch('batch','scope','revision',turns)
        self.shard=Shard((Span(2,0,4),),(Span(1,0,4),Span(3,0,4)))
        self.payload={'facts':[{'id':'f1','kind':'event','claim':'我喝了水','basis':'explicit',
                               'citations':[{'span_id':'2:0:4','quote':'我喝了水'}]}],
                      'omissions':[{'span_id':'1:0:4','reason':'context only'},
                                   {'span_id':'3:0:4','reason':'context only'}]}

    def resolve(self):
        return resolve_quotes(json.dumps(self.payload,ensure_ascii=False),self.batch,self.shard)

    def test_known_context_omissions_do_not_poison_owned_evidence(self):
        result=self.resolve()
        self.assertEqual(len(result['facts']),1)
        self.assertEqual(result['facts'][0]['citations'][0]['turn_id'],2)
        self.assertEqual([r['method'] for r in result['compatibility_repairs']],
                         ['ignored_context_only_omission']*2)
        self.assertEqual([r['span_id'] for r in result['coverage']],['2:0:4'])

    def test_context_still_cannot_supply_evidence(self):
        self.payload['facts'][0]['citations']=[{'span_id':'1:0:4','quote':'前文参考'}]
        with self.assertRaises(SourceError):self.resolve()

    def test_unknown_omission_is_not_ignored(self):
        self.payload['omissions'].append({'span_id':'999:0:4','reason':'context only'})
        with self.assertRaises(SourceError):self.resolve()

    def test_owned_contradictory_omission_is_not_ignored(self):
        self.payload['omissions'].append({'span_id':'2:0:4','reason':'context only'})
        with self.assertRaises(SourceError):self.resolve()

    def test_context_omissions_cannot_fill_missing_owned_coverage(self):
        self.payload['facts']=[]
        with self.assertRaises(SourceError):self.resolve()

    def test_context_reason_must_remain_bounded_and_valid(self):
        self.payload['omissions'][0]['reason']=''
        with self.assertRaises(SourceError):self.resolve()

    def test_numeric_id_is_losslessly_canonicalized(self):
        self.payload['facts'][0]['id']=1
        result=self.resolve()
        self.assertEqual(result['facts'][0]['id'],'1')
        self.assertEqual(result['coverage'][0]['fact_ids'],['1'])
        self.assertEqual(self.payload['facts'][0]['id'],1)

    def test_string_integer_identity_collision_is_rejected(self):
        self.payload['facts'][0]['id']=1
        self.payload['facts'].append({**self.payload['facts'][0],'id':'1'})
        with self.assertRaises(SourceError):self.resolve()

    def test_bool_and_float_ids_are_not_coerced(self):
        for value in (True,1.0,-1):
            self.payload['facts'][0]['id']=value
            with self.assertRaises(SourceError):self.resolve()

    def test_numeric_ids_can_enter_exact_citation_repair(self):
        self.payload['facts'][0]['id']=1
        self.payload['facts'][0]['citations'][0]['quote']='我喝了茶'
        raw=json.dumps(self.payload,ensure_ascii=False)
        payload=request(raw,self.batch,self.shard)
        self.assertEqual(payload['repairs'][0]['fact_id'],'1')
        result=apply(raw,json.dumps({'repairs':[{'fact_id':'1','citation_index':0,'quote':'我喝了水'}]},ensure_ascii=False),
                     payload,self.batch,self.shard)
        self.assertEqual(result['facts'][0]['citations'][0]['quote'],'我喝了水')
        self.assertEqual(result['facts'][0]['id'],'1')
        self.assertEqual(result['compatibility_repairs'][0]['method'],'integer_id_to_string')

    def test_escaped_quotes_resolve_to_actual_source_positions(self):
        source='他说\\"见见世面\\"的话。'
        spans,method=resolve(source,0,len(source),'他说"见见世面"的话。')
        self.assertEqual(spans,[(0,len(source))])
        self.assertEqual(method,'typography')

    def test_quote_ending_at_escape_keeps_actual_quote_character(self):
        source='他说\\"见见世面\\"然后走了。'
        spans,_=resolve(source,0,len(source),'"见见世面"')
        self.assertEqual(folded(source[spans[0][0]:spans[0][1]])[0],'"见见世面"')

    def test_unrelated_backslash_is_not_removed(self):
        with self.assertRaises(SourceError):resolve('路径a\\b',0,5,'路径ab')

    def test_unique_owned_quote_survives_wrong_optional_hints(self):
        self.payload['facts'][0]['citations'][0]['before']='不存在的提示'
        result=self.resolve()
        self.assertEqual(result['facts'][0]['citations'][0]['quote'],'我喝了水')
        self.assertEqual(result['compatibility_repairs'][0]['method'],'unique_owned_quote_ignored_invalid_hints')

    def test_wrong_hints_cannot_rescue_ambiguous_quote(self):
        turn=self.batch.turns[1]
        doubled=Turn(turn.id,turn.index,turn.role,'我喝了水我喝了水',turn.event_ts,turn.timezone,
                     hashlib.sha256('我喝了水我喝了水'.encode()).hexdigest())
        self.batch=SourceBatch('batch','scope','revision',(self.batch.turns[0],doubled,self.batch.turns[2]))
        self.shard=Shard((Span(2,0,8),),self.shard.context)
        self.payload['facts'][0]['citations'][0].update(span_id='2:0:8',before='不存在的提示')
        with self.assertRaises(SourceError):self.resolve()


if __name__=='__main__':unittest.main()
