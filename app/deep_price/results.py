"""Bounded in-memory session results. No business persistence or browser prices."""
import copy
import csv
import hashlib
import io
import json
import secrets
import threading
import time

from .errors import DeepPriceError


class ResultCache:
    def __init__(self, clock=time.monotonic, ttl=1800):
        self.clock,self.ttl = clock,ttl
        self.lock = threading.RLock()
        self.sessions,self.results = {},{}

    def session(self, candidate, principal):
        with self.lock:
            self.cleanup()
            record = self.sessions.get(candidate)
            if not record or record['principal'] != principal:
                candidate = secrets.token_urlsafe(32)
                if len(self.sessions)>=1000:
                    self.logout(min(self.sessions,key=lambda k:self.sessions[k]['seen']))
                record = {'principal':principal,'seen':self.clock()}
                self.sessions[candidate] = record
            record['seen'] = self.clock()
            return candidate

    def cleanup(self):
        now = self.clock()
        for key in list(self.results):
            if now-self.results[key]['created']>=self.ttl:
                del self.results[key]
        for key in list(self.sessions):
            if now-self.sessions[key]['seen']>=3600:
                self.logout(key)

    def logout(self, sid):
        with self.lock:
            self.sessions.pop(sid,None)
            for key in list(self.results):
                if self.results[key]['session']==sid:
                    del self.results[key]

    def put(self,sid,kind,result):
        with self.lock:
            self.cleanup()
            owned = [k for k,v in self.results.items() if v['session']==sid]
            if len(owned)>=100:
                del self.results[min(owned,key=lambda k:self.results[k]['created'])]
            if len(self.results)>=1000:
                del self.results[min(self.results,key=lambda k:self.results[k]['created'])]
            key = secrets.token_urlsafe(24)
            record = copy.deepcopy(result)
            record.update(result_id=key,result_kind=kind,result_ttl_seconds=self.ttl)
            self.results[key] = {'created':self.clock(),'session':sid,'result':record}
            return record

    def get(self,sid,result_id):
        if not isinstance(result_id,str):
            raise DeepPriceError('invalid_input','result_id必须为字符串')
        with self.lock:
            self.cleanup()
            item = self.results.get(result_id)
            if item is None or item['session']!=sid:
                raise DeepPriceError('not_found','结果不存在、已过期或不属于本次会话',404)
            return copy.deepcopy(item['result'])


def export_result(result,kind):
    if kind not in ('csv','txt'):
        raise DeepPriceError('invalid_input','导出格式仅支持csv或txt')
    if kind=='txt':
        return json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8'),'text/plain; charset=utf-8'
    rows = []
    def flatten(value,path=''):
        if isinstance(value,dict):
            for key,item in value.items():
                flatten(item,path+'.'+str(key) if path else str(key))
        elif isinstance(value,list):
            for index,item in enumerate(value):
                flatten(item,path+'[%d]'%index)
        else:
            rows.append((path,value))
    flatten(result)
    def safe(value):
        text = '' if value is None else str(value)
        return "'"+text if isinstance(value,str) and (text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n'))) else text
    output=io.StringIO(newline='')
    writer=csv.writer(output)
    writer.writerow(['字段 / 来源','值（价格单位：万元）'])
    writer.writerows((safe(k),safe(v)) for k,v in rows)
    return output.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8'
