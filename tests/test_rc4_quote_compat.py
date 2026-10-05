import unittest
from astrbot_plugin_memos_memory.generation_v2.quote_compat import resolve
from astrbot_plugin_memos_memory.generation_v2.sources import SourceError


class QuoteCompatibilityTests(unittest.TestCase):
    def test_typography_restores_source_not_model_text(self):
        source='他说"这件事我答应了"。\n\n明天我们一起出门。'
        spans,method=resolve(source,0,len(source),'他说“这件事我答应了”。明天我们一起出门。')
        self.assertEqual(method,'typography')
        self.assertEqual([source[a:b] for a,b in spans],[source])

    def test_ordered_excerpts_keep_separate_exact_offsets(self):
        first='我已经答应你了呀。';last='明天我们一起出门吧。'
        source=first+'\n（她停了一下，轻轻点头）\n'+last
        spans,method=resolve(source,0,len(source),first+last)
        self.assertEqual(method,'ordered_excerpts')
        self.assertEqual([source[a:b] for a,b in spans],[first,last])

    def test_rewrites_missing_negation_and_word_boundaries_fail(self):
        for source,quote in [('我没有答应明天出门。','我答应明天出门。'),
                             ('I am not able to leave.','I am notable to leave.'),
                             ('今天我们不会一起出门。','今天我们会一起出门。')]:
            with self.assertRaises(SourceError):resolve(source,0,len(source),quote)

    def test_ambiguity_reversal_and_large_gap_fail(self):
        a='我已经答应你了呀。';b='明天我们一起出门吧。'
        for source in (a+b+a+b,b+'（停下）'+a,a+'中'*241+b):
            with self.assertRaises(SourceError):resolve(source,0,len(source),a+b)

    def test_context_and_owned_boundary_remain_enforced(self):
        source='开头“明天我们一起出门”。结尾'
        quote='"明天我们一起出门"。'
        with self.assertRaises(SourceError):resolve(source,3,len(source),quote)
        with self.assertRaises(SourceError):resolve(source,0,len(source),quote,before='另一段')
        spans,_=resolve(source,0,len(source),quote,before='开头',after='结尾')
        self.assertEqual(source[spans[0][0]:spans[0][1]],'“明天我们一起出门”。')
