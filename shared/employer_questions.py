"""Read public employer question metadata; never submit through this module."""
import re
from urllib.parse import urlsplit

SOURCE_KEY = 'Application discovery source'


def question_key(label):
    return re.sub(r'[^a-z0-9]+', ' ', label.lower()).strip()


def greenhouse_questions_url(url):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname not in ('job-boards.greenhouse.io','boards.greenhouse.io') or parsed.username or parsed.password:
        return None
    match=re.fullmatch(r'/([\w-]+)/jobs/(\d+)/?',parsed.path)
    if not match: return None
    return f'https://boards-api.greenhouse.io/v1/boards/{match[1]}/jobs/{match[2]}?questions=true'


def schema_questions(data):
    result=[]
    for q in data.get('questions',[]):
        label=q.get('label','').strip()
        if not label or len(label)>300: continue
        fields=q.get('fields',[])
        if not fields or any(f.get('type') in ('input_file','input_hidden') for f in fields): continue
        options=list(dict.fromkeys(str(v['label']) for f in fields for v in f.get('values',[]) if v.get('label')))
        if len(options)>300 or any(len(v)>300 for v in options): continue
        result.append({'label':label,'options':options,'required':bool(q.get('required'))})
    return result


def discovery_answer(label, options, answers):
    if answers.get(SOURCE_KEY)!='Internship Radar': return None
    if not re.search(r'how did you (?:hear|learn|find)|where did you (?:hear|learn|find)',label,re.I): return None
    if not options: return 'Internship Radar'
    return next((o for o in options if question_key(o) in ('other','other please specify','internship radar')),None)


def refresh_questions(existing, schema):
    """Enrich recorded missing fields and repair checkbox options mistaken for labels."""
    indexed={question_key(q['label']):q for q in schema}
    groups=[]
    def clean(label):
        return label.removesuffix(' (unsupported control or answer)').removesuffix(' (choose an exact option)')
    recorded={question_key(clean(q['label'] if isinstance(q,dict) else q)) for q in existing}
    for q in schema:
        overlap=len(set(map(question_key,q['options'])) & recorded)
        if overlap>=3 or (overlap and re.search(r'how did you hear',q['label'],re.I)):
            groups.append(q)
    misplaced={question_key(o) for q in groups for o in q['options']}
    result=[]
    for old in existing:
        label=clean(old['label'] if isinstance(old,dict) else old)
        key=question_key(label)
        if key in misplaced: continue
        match=indexed.get(key)
        result.append({'label':label,'options':match['options']} if match else old)
    for group in groups:
        if question_key(group['label']) not in recorded:
            result.append({'label':group['label'],'options':group['options']})
    return result
