"""Quotation IDs are exact source references, never fuzzy repairs or fabricated evidence."""
import json
import unittest
from unittest.mock import MagicMock, patch

from app.ai import ModelClient, validate_entry
from app.prompts import ENTRY_PROMPT
from app.source_refs import entry_references, day_references, segments


class SourceReferenceTests(unittest.TestCase):
    def test_segments_roundtrip_noisy_unicode_original_without_normalization(self):
        body='哈哈哈哈哈哈哈，ChatBI 🌿！\n空 格与错别字；'+'很长的原话哈哈哈'*130
        pieces=segments(body)
        self.assertEqual(''.join(pieces),body)
        self.assertTrue(all(0<len(p)<=240 for p in pieces))

    def test_restored_quote_comes_only_from_selected_source(self):
        body='今天觉得很开心哈哈哈哈哈。\n明天整理资料。'
        wire,decode=entry_references({'entry':{'id':'r1','body':body}})
        self.assertNotIn('body',wire['entry'])
        self.assertEqual(''.join(s['text'] for s in wire['entry']['segments']),body)
        result=decode({'insights':[{'type':'fact','quote_id':'q1','quote':'模型改写的错误文字'}]})
        self.assertEqual(result['insights'][0]['quote'],'今天觉得很开心哈哈哈哈哈。')
        self.assertNotIn('quote_id',result['insights'][0])

    def test_unknown_or_cross_record_reference_is_rejected(self):
        _,decode=entry_references({'entry':{'id':'r1','body':'原文。'}})
        for source in ({'quote_id':'q99'},{'quote_id':'q1','record_id':'other'},{'quote_id':['q1']}):
            with self.assertRaises(ValueError): decode(source)

    def test_legacy_quote_is_not_fuzzily_repaired(self):
        _,decode=entry_references({'entry':{'id':'r1','body':'原文哈哈哈。'}})
        self.assertEqual(decode({'quote':'原文哈哈。'}),{'quote':'原文哈哈。'})

    def test_daily_segments_are_bound_to_their_own_source_records(self):
        wire,decode=day_references({'records':[{'id':'r1','body':'第一条哈哈哈。'},{'id':'r2','body':'第二条，保留错别字。'}]})
        self.assertEqual(decode({'entry_id':'r2','quote_id':'q2'})['quote'],'第二条，保留错别字。')
        self.assertEqual(len(wire['records']),2)
        for value in ({'entry_id':'r1','quote_id':'q2'},{'quote_id':'q1'}):
            with self.assertRaises(ValueError): decode(value)

    def test_real_client_wire_uses_segments_and_restores_before_strict_validator(self):
        entry={'id':'r1','body':'今天整理了项目资料哈哈哈哈哈。','journal_day':'2026-10-26'}
        answer={'schema_version':1,'summary':'整理了项目资料','insights':[{'text':'整理项目资料','type':'fact','quote_id':'q1'}],'actions':[],'completions':[]}
        response=MagicMock()
        response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps(answer)}}]}).encode()
        opener=MagicMock();opener.open.return_value=response
        client=ModelClient(type('Keys',(),{'get':lambda _:''})())
        with patch('urllib.request.build_opener',return_value=opener):
            output=client.call({'ai_mode':'local','ai_model':'synthetic','ai_base_url':'http://127.0.0.1:1/v1'},ENTRY_PROMPT,{'entry':entry})
        wire=json.loads(opener.open.call_args.args[0].data)
        self.assertIn('segments',json.loads(wire['messages'][1]['content'])['entry'])
        self.assertEqual(validate_entry(output,entry,[])['insights'][0]['quote'],entry['body'])


if __name__=='__main__': unittest.main()
