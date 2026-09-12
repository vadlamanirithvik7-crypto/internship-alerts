"""Consolidate unanswered questions without merging different employer choices."""
import hashlib
import json
import re
from shared.auto_input import question_view
from shared.profile_answers import combined_answers, resolved_answer, known_answer, concept
from shared.employer_questions import question_key


def task_view(task, profile, shared):
    view=question_view(task)
    answers=combined_answers(json.loads(task.answers or '{}'),shared)
    resolved={}
    fields=[]
    for field in view['fields']:
        value=resolved_answer(field['label'],field['options'],profile,answers)
        if value is None and concept(field['label'])=='school' and len(field['options'])>=100:
            # Large searchable menus often capture only their first page. The
            # worker must search/verify this known school, not ask it again.
            value=known_answer(field['label'],profile,answers)
        if value is None: fields.append(field)
        else: resolved[field['label']]=value
    known_issues={}
    for reason in view['blockers']:
        if reason.endswith(' (unsupported control or answer)'):
            label=reason.removesuffix(' (unsupported control or answer)')
            value=known_answer(label,profile,answers)
            if value: known_issues[label]=value
    view.update(fields=fields,resolved=resolved,known_issues=known_issues)
    if resolved:
        view['version']=hashlib.sha256((view['version']+json.dumps(resolved,sort_keys=True)).encode()).hexdigest()
    if task.state=='needs_input':
        view['label']='Needs answers' if fields else ('Needs manual review' if view['blockers'] else 'Saved answers ready')
        view['action']='Answer questions' if fields else ('Review issue' if view['blockers'] else 'Continue with saved answers')
    return view


def inbox(tasks, postings, profile, shared):
    groups={}; known=0; issues=0
    for task in tasks:
        view=task_view(task,profile,shared)
        known+=len(view['resolved'])
        if view['blockers']: issues+=1
        for field in view['fields']:
            semantic=concept(field['label']) or question_key(field['label'])
            scoped=bool(re.search(r'\bwhy\b|this (?:role|position|company)|our (?:team|company)|employee referral|referred by|privacy|terms and conditions|consent|certif|acknowledge|code of conduct|pick date|select date|choose date',field['label'],re.I))
            # Keep employer-specific wording and different choices separate.
            key=hashlib.sha256(json.dumps([semantic,sorted(field['options']),task.posting_id if scoped else None]).encode()).hexdigest()[:24]
            group=groups.setdefault(key,{'id':key,'label':field['label'],'options':field['options'],
                                        'saved':known_answer(field['label'],profile,shared),'reusable':not scoped,'entries':[]})
            posting=postings[task.posting_id]
            group['entries'].append({'task_id':task.id,'label':field['label'],'company':posting.company_name,'title':posting.title})
    return {'groups':sorted(groups.values(),key=lambda g:(-len(g['entries']),g['label'])),
            'known':known,'issues':issues}
