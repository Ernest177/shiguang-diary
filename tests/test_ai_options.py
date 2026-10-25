"""Provider-specific reasoning settings, source-bound receipts and uncertain timeout handling."""
import tempfile
import json
import unittest
from unittest.mock import patch

from app.ai import AITimeout, Worker
from app.request_context import receipt_key
from app.request_options import request_options
from app.store import Store


class OptionsTests(unittest.TestCase):
    def test_prompt_examples_use_real_enum_values_instead_of_choice_descriptions(self):
        from app import prompts
        def example(prompt):
            return json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
        entry=example(prompts.ENTRY_PROMPT)
        self.assertIn(entry['insights'][0]['type'],('fact','idea'))
        self.assertIn(entry['actions'][0]['kind'],('task','opportunity','question'))
        self.assertIn(example(prompts.DAY_PROMPT)['sections'][0]['category'],('events','insights','decisions','actions','opportunities'))
        self.assertIn(example(prompts.GROWTH_PROMPT)['memories'][0]['category'],('fact','value','preference','goal'))
        self.assertIn(example(prompts.MEMORY_PROMPT)['memories'][0]['horizon'],('long_term','recent','unknown'))
        self.assertIn(example(prompts.PORTRAIT_PROMPT)['items'][0]['certainty'],('observation','hypothesis'))
        self.assertIn(example(prompts.MONTH_PROMPT)['items'][0]['category'],('events','growth','questions'))

    def test_only_known_hybrid_qwen_on_aliyun_disables_thinking(self):
        settings={'ai_base_url':'https://synthetic.cn-beijing.maas.aliyuncs.com/compatible-mode/v1','ai_model':'qwen3.8-flash'}
        self.assertIs(request_options(settings)['enable_thinking'],False)
        for model in ('glm-5.3','qwen3.8-2.4t-a95b','qwen3.7-max-preview','qwen3.7-max-2026-05-17','custom-model'):
            self.assertNotIn('enable_thinking',request_options({**settings,'ai_model':model}))
        self.assertNotIn('enable_thinking',request_options({**settings,'ai_base_url':'https://provider.example/v1'}))

    def test_unsupported_deadline_is_discarded_and_explicit_local_duration_still_recovers(self):
        from app.ai import validate_entry
        entry={'body':'花两天时间整理项目思路','created_at':'2026-10-20T01:00:00+00:00','timezone':'Asia/Shanghai'}
        raw={'schema_version':1,'summary':'整理项目计划','insights':[],'completions':[],
             'actions':[{'title':'整理项目思路','kind':'task','quote':entry['body'],'due_text':'未来两天完成','due_date':'2099-12-31','existing_action_id':None}]}
        action=validate_entry(raw,entry,[])['actions'][0]
        self.assertEqual(action['due_date'],'2026-10-22')
        self.assertIn(action['due_text'],entry['body'])
        entry['body']='有时间整理项目思路';raw['actions'][0]['quote']=entry['body']
        action=validate_entry(raw,entry,[])['actions'][0]
        self.assertIsNone(action['due_date']);self.assertIsNone(action['due_text'])

    def test_receipt_includes_effective_request_options(self):
        settings={'ai_mode':'local','ai_model':'synthetic','ai_base_url':'http://127.0.0.1:1/v1'}
        context={'entry':{'id':'synthetic','body':'合成记录'},'existing_actions':[]}
        first=receipt_key('entry',context,settings)
        with patch('app.request_context.request_options',return_value={'temperature':.2,'stream':False,'enable_thinking':False}):
            self.assertNotEqual(first,receipt_key('entry',context,settings))

    def test_timeout_stops_blind_retry_but_explicit_retry_remains_available(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            store.save_settings({'ai_mode':'local','ai_model':'synthetic','ai_base_url':'http://127.0.0.1:1/v1'})
            entry=store.save_entry({'body':'合成记录','journal_day':'2026-10-26'})
            with store.connection() as db: db.execute("DELETE FROM jobs WHERE kind!='entry'")
            class Client:
                calls=0
                def call(self,*args):
                    self.calls+=1
                    raise AITimeout('合成超时')
            client=Client(); worker=Worker(store,client)
            self.assertTrue(worker.step()); self.assertFalse(worker.step())
            self.assertEqual(client.calls,1)
            with store.connection() as db:
                row=db.execute("SELECT status,attempts FROM jobs WHERE kind='entry'").fetchone()
                self.assertEqual(tuple(row),('failed',1))
            store.retry(entry_id=entry['id'])
            worker.step(); self.assertEqual(client.calls,2)


if __name__=='__main__': unittest.main()
