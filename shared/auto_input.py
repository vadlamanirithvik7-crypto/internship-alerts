"""Turn worker diagnostics into answerable questions or manual-review reasons."""
import hashlib
import json


def question_view(task):
    fields, blockers = [], []
    diagnostics = ('could not ', 'the employer ', 'the application ', 'employer ', 'this employer ',
                   'this application ', 'this role ', 'this link ', 'this employer application ',
                   'an exact employer ', 'multiple ', 'some required ', 'unlabeled ',
                   'unidentified ', 'required attachment:', 'browser ', 'existing confirmation ')
    for value in json.loads(task.questions or '[]'):
        question = value.get('label','') if isinstance(value,dict) else str(value)
        if not question.strip(): continue
        lower = question.lower()
        # Real questions can begin with "This role"; only diagnostic sentences
        # recorded by the worker have these exact openings / control suffixes.
        manual = (not isinstance(value,dict) and lower.startswith(diagnostics) and not question.rstrip().endswith(('?', '*')))
        manual |= lower.strip(' *') in ('cover letter','resume','cv')
        manual |= lower.startswith('required attachment:')
        manual |= '(unsupported control or answer)' in lower
        if manual:
            blockers.append(question)
            continue
        label = question.removesuffix(' (choose an exact option)')
        options = value.get('options',[]) if isinstance(value,dict) else []
        if not any(f['label']==label for f in fields):
            fields.append({'label':label,'options':options})
    if task.state=='needs_input' and not fields and not blockers:
        blockers.append(task.detail or 'The employer form could not be completed automatically.')
    label = ('Needs answers' if fields else 'Needs manual review') if task.state=='needs_input' else task.state.replace('_',' ').title()
    return {'fields':fields,'blockers':blockers,'label':label,
            'action':'Answer questions' if fields else 'Review issue',
            'version':hashlib.sha256((task.questions or '[]').encode()).hexdigest()}


def pack_questions(questions, details):
    result=[]
    for question in questions[:80]:
        label=question.removesuffix(' (choose an exact option)')
        if label in details:
            result.append({'label':label[:300],'options':details[label][:300]})
        else:
            result.append(question[:300])
    # Keep all question labels if unusually large dropdown lists exceed the
    # worker request budget. Those fields fall back to typed answers.
    while len(json.dumps(result).encode())>50000:
        candidates=[q for q in result if isinstance(q,dict) and q['options']]
        if not candidates: break
        max(candidates,key=lambda q:len(json.dumps(q['options'])))['options']=[]
    return result


def validate_questions(questions):
    if not isinstance(questions,list) or len(questions)>80:
        raise ValueError('Invalid questions.')
    result=[]
    for question in questions:
        if isinstance(question,dict):
            label=question.get('label'); options=question.get('options',[])
            if not isinstance(label,str) or not label.strip() or len(label)>300:
                raise ValueError('Invalid question label.')
            if not isinstance(options,list) or len(options)>300 or any(not isinstance(o,str) or len(o)>300 for o in options):
                raise ValueError('Invalid answer choices.')
            result.append({'label':label,'options':list(dict.fromkeys(options))})
        else: result.append(str(question)[:300])
    return result
