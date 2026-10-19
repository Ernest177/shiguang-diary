"""Explicitly labeled local HTTP fixture for manual UI QA. Not a real AI model."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Fixture(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        content=json.loads(request['messages'][-1]['content'])
        if 'entry' in content:
            body=content['entry']['body']
            actions=[]
            for phrase,kind,title in [('明天联系客户确认需求','task','联系客户确认需求'),('也许值得做一个资料归档模板','opportunity','探索资料归档模板')]:
                if phrase in body:
                    actions.append({'title':title,'kind':kind,'quote':phrase,'due_text':None,'due_date':None,'existing_action_id':None})
            result={'schema_version':1,'summary':'【验收替身，非真实 AI】保存了资料整理经验与跟进计划。','insights':[{'text':'【验收替身】按项目归档的想法','quote':body,'type':'idea'}],'actions':actions,'completions':[]}
        elif 'records' in content:
            e=content['records'][0]
            result={'schema_version':1,'overview':'【验收替身，非真实 AI】这里用于检查每日回顾的布局、来源链接和状态更新。','sections':[{'category':'insights','items':[{'text':'【验收替身】记录了归档经验与灵感。','sources':[{'entry_id':e['id'],'quote':e['body']}]}]}]}
        else:
            result={'ok':True}
        raw=json.dumps({'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]},ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


if __name__=='__main__':
    HTTPServer(('127.0.0.1',8767),Fixture).serve_forever()
