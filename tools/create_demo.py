"""Create synthetic, no-network product demonstrations in a separate database."""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.store import Store, compact, now, tzinfo, uid
from app.ai import Worker


class PersonalDemoModel:
    """Handwritten response fixture; never contacts an AI provider."""
    def call(self, settings, prompt, context):
        if "memories" in context:
            memories = context["memories"]
            return {"schema_version":1,"items":[{"text":m["detail"],"category":{"fact":"strengths","preference":"preferences","value":"values"}.get(m["category"],"focus"),
                    "certainty":"observation","memory_ids":[m["id"]]} for m in memories]}
        records = context["records"]
        if "month" in context:
            current = next(r for r in records if r["period"].startswith(context["month"]))
            previous = next((r for r in records if r["period"].startswith(context["previous_month"])),None)
            refs = [{"record_id":current["id"],"quote":current["body"]}]
            if previous:
                refs.append({"record_id":previous["id"],"quote":previous["body"]})
            return {"schema_version":1,"items":[{"text":"【合成示例，未调用模型】从提前准备表达，到为想法留下整理空间，可以回看这两段不同阶段的尝试。变化的意义仍由你确认。",
                    "category":"growth","comparison":bool(previous),"sources":refs}]}
        output = []
        for title, label, category, phrase in [
            ("提前准备，让表达更从容","【合成示例】曾经通过提前列要点，主动表达建议。","fact","提前列了三个要点，在讨论中主动表达了自己的建议"),
            ("给想法留出整理的时间","【合成示例】倾向先记录，再留时间慢慢整理。","preference","更喜欢先保存，再慢慢整理"),
            ("允许自己慢慢变化","【合成示例】希望为自己的变化留出空间。","value","不急着给自己下结论")]:
            record = next((r for r in records if phrase in r["body"]),None)
            if record:
                output.append({"title":title,"detail":label,"category":category,"horizon":"recent" if category=="fact" else "unknown",
                               "period":record["period"],"sources":[{"record_id":record["id"],"quote":phrase}]})
        return {"schema_version":1,"memories":output}


def add_personal_demo(store):
    month = store.today()[:7]
    year,number=map(int,month.split('-'))
    previous=f'{year-(number==1):04d}-{12 if number==1 else number-1:02d}'
    store.save_experience({'title':'在讨论中，第一次主动提出方案','period':previous,'chapter':'成长',
                          'body':'【合成演示，非真实个人经历】以前常在讨论结束后才补充想法。那一次，我提前列了三个要点，在讨论中主动表达了自己的建议。','ai_enabled':True})
    store.save_experience({'title':'给零散想法，一个安放的地方','period':month,'chapter':'生活',
                          'body':'【合成演示，非真实个人经历】这个月开始随手记录。我更喜欢先保存，再慢慢整理，也不急着给自己下结论。','ai_enabled':True})
    store.save_settings({'ai_mode':'local','ai_base_url':'http://127.0.0.1:1/v1','ai_model':'合成展示数据 · 非真实 AI'})
    store.request_about('memory')
    worker=Worker(store,PersonalDemoModel())
    worker.step();worker.step()
    memory=next((m for m in store.memories() if m['category']=='fact'),None)
    if memory:
        store.update_memory(memory['id'],{'status':'confirmed','updated_at':memory['updated_at']})
        store.request_about('portrait');worker.step()
    store.request_about('month',month);worker.step()
    store.save_settings({'ai_mode':'off'})


def create_demo(directory):
    directory = Path(directory).resolve()
    real_directory = (ROOT / 'data').resolve()
    if directory == real_directory or directory.is_relative_to(real_directory):
        raise ValueError('Refusing to create demo fixtures inside the real data directory')
    if (directory / 'journal.sqlite3').exists():
        raise ValueError('Demo database already exists; refusing to overwrite it')
    store = Store(directory)
    day = store.today()
    marker = '【合成演示，非真实个人日记】'
    samples = [
        {'body': marker+'今天重新整理了设计资料，按项目归档比按文件类型更容易找到。也许值得做一个可复用的资料归档模板。',
         'summary':'【合成示例，未调用模型】留下了资料归档经验和一个模板想法。',
         'insight':('按项目归档更容易查找。','按项目归档比按文件类型更容易找到','idea'),
         'action':('探索可复用的资料归档模板','opportunity','也许值得做一个可复用的资料归档模板',None,None)},
        {'body': marker+'明天联系客户确认页面需求。今天已经把旧版方案发出去了，不准备重做整套视觉。',
         'summary':'【合成示例，未调用模型】保留一个明确跟进计划，已完成和否定事项不生成待办。',
         'insight':('旧版方案已发出。','今天已经把旧版方案发出去了','fact'),
         'action':('联系客户确认页面需求','task','明天联系客户确认页面需求','明天',(datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat())},
        {'body': marker+'决定让日记先保存，再在后台整理。这样即使模型暂时不可用，也不影响把想法记下来。',
         'summary':'【合成示例，未调用模型】记录了原文先保存的设计决定。',
         'insight':('先保存原文，再异步整理。','决定让日记先保存，再在后台整理','fact'),
         'action':None}
    ]
    entries=[]
    for index,sample in enumerate(samples):
        e=store.save_entry({'body':sample['body'],'journal_day':day})
        stamp=datetime.now(tzinfo('Asia/Shanghai')).replace(hour=9+index*3,minute=12+index*9,second=0,microsecond=0).astimezone(tzinfo('UTC')).isoformat()
        with store.connection() as db:
            db.execute('UPDATE entries SET created_at=?,updated_at=? WHERE id=?',(stamp,stamp,e['id']))
            db.execute('UPDATE entry_versions SET saved_at=? WHERE entry_id=?',(stamp,e['id']))
            insight_text,quote,kind=sample['insight']
            result={'schema_version':1,'summary':sample['summary'],'insights':[{'text':insight_text,'quote':quote,'type':kind}],'actions':[],'completions':[]}
            if sample['action']:
                title,action_kind,action_quote,due_text,due_date=sample['action']
                result['actions'].append({'title':title,'kind':action_kind,'quote':action_quote,'due_text':due_text,'due_date':due_date,'existing_action_id':None})
                action_id=uid()
                fingerprint=hashlib.sha256((action_kind+':'+title).encode()).hexdigest()
                db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)',(action_id,fingerprint,title,action_kind,'open',due_text,due_date,0,stamp,stamp))
                db.execute('INSERT INTO action_sources VALUES(?,?,?,?)',(action_id,e['id'],1,action_quote))
            db.execute('INSERT INTO analyses VALUES(?,?,?,?,?)',(e['id'],1,compact(result),'合成展示数据 · 非真实 AI',stamp))
        entries.append(e)
    context,signature=store.day_context(day)
    data={'schema_version':1,'overview':'【合成示例，未调用模型】这一天留下了归档经验、一个明确的客户跟进计划，以及原文先保存的产品决定。',
          'sections':[
              {'category':'insights','items':[{'text':'按项目归档更方便查找资料。','sources':[{'entry_id':entries[0]['id'],'quote':'按项目归档比按文件类型更容易找到'}]}]},
              {'category':'decisions','items':[{'text':'记录先保存，AI 在后台整理。','sources':[{'entry_id':entries[2]['id'],'quote':'决定让日记先保存，再在后台整理'}]}]},
              {'category':'actions','items':[{'text':'明天联系客户确认页面需求。','sources':[{'entry_id':entries[1]['id'],'quote':'明天联系客户确认页面需求'}]}]},
              {'category':'opportunities','items':[{'text':'可以考虑探索资料归档模板。','sources':[{'entry_id':entries[0]['id'],'quote':'也许值得做一个可复用的资料归档模板'}]}]}]}
    with store.connection() as db:
        db.execute('INSERT INTO daily_summaries VALUES(?,?,?,?,?)',(day,signature,compact(data),'合成展示数据 · 非真实 AI',now()))
        db.execute("UPDATE jobs SET status='done'")
    add_personal_demo(store)
    # Verify the example satisfies the same import contracts as actual application data.
    store.validate_import(store.export())
    return directory


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Create clearly labeled synthetic demo data; no AI requests')
    parser.add_argument('--output',default=str(ROOT/'.demo-data'))
    arguments=parser.parse_args()
    try:
        directory=create_demo(arguments.output)
    except ValueError as error:
        parser.exit(1,str(error)+'\n')
    print('Synthetic demo created (no AI calls): '+str(directory))
    print('Run: python app/server.py --port 8766 --data-dir "'+str(directory)+'"')
