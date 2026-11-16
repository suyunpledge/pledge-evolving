"""Audit usage, hash-only replay provenance and identical-wire cache variation."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT))
from verify_deepseek_execution import totals

records=[];replay=[];variation=[];source_paths=set();errors=[]
for path in sorted((ROOT/'reports').glob('cache-diagnostics-*.json')):
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data.get('requests'),list) or 'totals' not in data:continue
    rows=data['requests'];computed=totals(rows)
    if computed!=data['totals']:errors.append(path.name+': totals mismatch')
    groups={};sources={};checks={'reasoning_content':0,'encrypted_content':0,'response_call_bindings':0,'required_field_presence':0}
    for row in rows:
        for message in row.get('message_components',[]):
            tool_group=message['fields'].get('tool_calls',{}).get('digest')
            for field in sources.get(tool_group,set()):
                checks['required_field_presence']+=1
                if 'matches_prior_response' not in message['fields'].get(field,{}):
                    errors.append(path.name+': required replay field missing')
            for field in ('reasoning_content','encrypted_content'):
                item=message['fields'].get(field,{})
                if 'matches_prior_response' in item:
                    checks[field]+=1
                    if not item['matches_prior_response']:errors.append(path.name+': replay source mismatch')
                if 'matches_response_call' in item:
                    checks['response_call_bindings']+=1
                    if not item['matches_response_call']:errors.append(path.name+': call-bound replay mismatch')
        for call in row.get('tool_calls') or []:
            function=call.get('function') or {}
            if function.get('name')=='read_file':
                try:source_paths.add(json.loads(function['arguments']).get('path'))
                except (ValueError,TypeError):errors.append(path.name+': unreadable read arguments')
        usage=row.get('usage') or {}
        if usage and usage.get('cache_observed'):
            raw=row.get('raw_usage') or {};details=raw.get('prompt_tokens_details') or {}
            if 'cached_tokens' in details and details['cached_tokens']!=usage['cached_tokens']:
                errors.append(path.name+': cache normalization mismatch')
        if row.get('wire_body_sha256') and usage.get('cache_observed'):
            groups.setdefault(row['wire_body_sha256'],[]).append(row)
        if row.get('tool_calls'):
            encoded=json.dumps(row['tool_calls'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            sources[hashlib.sha256(encoded).hexdigest()]={field for field,key in (
                ('reasoning_content','reasoning_chars'),('encrypted_content','encrypted_content_chars')) if row.get(key,0)>0}
    for fingerprint,group in groups.items():
        values=[r['usage']['cached_tokens'] for r in group]
        if 0 in values and any(v>0 for v in values):
            variation.append({'report':path.name,'wire_body_sha256':fingerprint,
                'cached_tokens':values,'input_tokens':[r['usage']['prompt_tokens'] for r in group],
                'stages':[r['stage'] for r in group],
                'same_affinity_key':len({r.get('affinity_key_digest') for r in group})==1,
                'same_generation_parameters':len({r['generation_parameters_digest'] for r in group})==1})
    replay.append({'report':path.name,**checks})
    records.append({'report':path.name,'ok':data.get('ok'),**{k:computed[k] for k in (
        'attempts','successful_requests','prompt_tokens','completion_tokens','cached_tokens')}})
if source_paths-{'values.json'}:errors.append('Unexpected source file outside synthetic fixture')
summary={k:sum(r[k] for r in records) for k in ('attempts','successful_requests','prompt_tokens','completion_tokens','cached_tokens')}
summary['total_tokens']=summary['prompt_tokens']+summary['completion_tokens']
result={'evidence_consistent':not errors,'errors':errors,'experiments':records,'totals':summary,
    'replay_checks':replay,'identical_wire_mixed_cache_receipts':variation,
    'observed_read_paths':sorted(source_paths),
    'scope':'Failed trials remain failed; consistent evidence is not a claim that every experiment passed.'}
(ROOT/'reports/cache-diagnostics-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'evidence_consistent':not errors,'reports':len(records),'identical_wire_variation_groups':len(variation),'totals':summary},ensure_ascii=False))
raise SystemExit(0 if not errors else 1)
