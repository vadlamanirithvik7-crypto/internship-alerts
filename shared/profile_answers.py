"""Deterministic reuse of applicant facts, shared by the UI and browser worker."""
import calendar
import re
from datetime import datetime
from shared.employer_questions import question_key

FIELDS = {
    'first_name':'First name','last_name':'Last name','email':'Application email','phone':'Phone number',
    'university_email': 'University email', 'school': 'School / university', 'degree': 'Degree', 'major': 'Major / discipline',
    'graduation_date': 'Graduation date', 'college_start_date': 'College start date',
    'gpa': 'GPA on a 4.0 scale', 'linkedin': 'LinkedIn URL', 'github': 'GitHub URL',
    'street': 'Street address', 'city': 'City', 'state': 'State', 'zip': 'ZIP / postal code',
    'country': 'Country', 'offers': 'Outstanding offers or deadlines',
    'office_preference': 'Office location preference',
}
PREFIX = 'Profile: '
ALIASES = {
    'first_name':['first name','given name','what is your legal first name'],'last_name':['last name','family name','surname'],
    'email':['email','email address'],'phone':['phone','phone number','mobile phone','mobile phone number'],
    'university_email':['alternate email','university email','university email address','please provide your university email address'],
    'school': ['school','school name','university','university name','college university','college or university','what university do you attend','which university are you currently attending','what school do you attend','name of university','current university','please re confirm the university you currently attend'],
    'degree': ['degree','degree type','degree level','what is your degree','what is the highest degree level you are currently pursuing','highest degree pursued','education level'],
    'major': ['major','major field','field of study','discipline','academic discipline','major discipline','what is your major','what is your field of study','major field of study','undergrad discipline s'],
    'graduation_date': ['graduation date','expected graduation date','anticipated graduation date','what is your expected graduation date','when do you expect to graduate','when will you graduate','please select your anticipated graduation date','what is your anticipated graduation month and year','when is your anticipated graduation date please select a graduation date range'],
    'college_start_date': ['college start date','education start date','university start date','what is your college start date'],
    'gpa': ['gpa','overall gpa','cumulative gpa','current gpa','what is your cumulative gpa','what is your gpa','what is your current cumulative gpa on a 4 0 scale','gpa on a 4 0 scale'],
    'linkedin': ['linkedin','linkedin url','linkedin link','linkedin profile','linkedin profile url','linkedin profile link','your linkedin profile','please provide your linkedin profile url'],
    'github': ['github','github url','github link','github profile','github profile url','github account','github profile link','please provide your github url'],
    'street': ['street address','address line 1','address 1'],
    'city': ['city','current city'], 'state': ['state','state province','state if n a select other'],
    'zip': ['zip','zip code','postal code','zip postal code','zipcode'], 'country': ['country','country of residence','what country do you currently reside in'],
    'offers': ['do you have any outstanding offers or deadlines','do you have any outstanding offers','outstanding offers or deadlines','do you currently have any offers'],
    'office_preference': ['what is your office location preference','office location preference','preferred office location'],
}
LOOKUP = {alias:key for key,aliases in ALIASES.items() for alias in aliases}


def concept(label):
    key=question_key(label)
    key=re.sub(r'\s+(?:select|choose) other if.*$', '', key)
    key=re.sub(r'\s+note the software engineer internship.*$', '', key)
    if key.startswith('internships at hudl are only open to currently enrolled college students '):
        key=key.split('students ',1)[1]
    if key in LOOKUP: return LOOKUP[key]
    if re.fullmatch(r'(?:what is your |expected |anticipated )?graduation (?:month|year)',key): return 'graduation_'+key.split()[-1]
    if key in ('end date month','end date year'): return 'graduation_'+key.split()[-1]
    if key in ('start date month','start date year'): return 'college_start_'+key.split()[-1]
    if key.startswith('please select your gpa range based on a 4 0 scale'): return 'gpa'
    return None


def profile_values(profile, answers):
    values={key:'' for key in FIELDS}
    for label,value in answers.items():
        key=concept(label)
        if key in values and isinstance(value,str) and value.strip(): values[key]=value.strip()
    for key in FIELDS:
        if answers.get(PREFIX+key): values[key]=str(answers[PREFIX+key])
    for key in ('first_name','last_name','email','phone','linkedin','github'):
        if profile.get(key): values[key]=profile[key]
    return values


def date_component(value, part):
    year=re.search(r'\b(20\d{2})\b',value)
    if part=='year': return year[1] if year else None
    for fmt in ('%Y-%m-%d','%B %d, %Y','%B %d %Y','%B %Y','%b %Y','%m/%d/%Y'):
        try: return calendar.month_name[datetime.strptime(value,fmt).month]
        except ValueError: pass
    return None


def known_answer(label, profile, answers):
    if question_key(label) in ('full name','name'):
        return (profile.get('first_name','')+' '+profile.get('last_name','')).strip() or None
    kind=concept(label)
    if not kind: return None
    values=profile_values(profile,answers)
    if kind.startswith('graduation_') and kind!='graduation_date':
        return date_component(values['graduation_date'],kind.rsplit('_',1)[1])
    if kind.startswith('college_start_') and kind!='college_start_date':
        return date_component(values['college_start_date'],kind.rsplit('_',1)[1])
    return values.get(kind) or None


def match_option(label, value, options):
    """Use exact meaning/format equivalents; leave ambiguous categories unanswered."""
    if value is None: return None
    if not options: return str(value)
    norm=question_key(str(value));kind=concept(label)
    matches=[o for o in options if question_key(o)==norm]
    if len(matches)==1: return matches[0]
    if kind=='country':
        def country_key(v):
            k=question_key(re.sub(r'\s*\+\d+$','',v))
            return 'united states' if k in ('united states of america','usa','us','u s','united states') else k
        matches=[o for o in options if country_key(o)==country_key(value)]
    elif kind=='degree':
        level=next((x for x in ('bachelor','master','doctor','associate') if x in norm),None)
        if norm in ('bs','b s','bsc','b sc','ba','b a'): level='bachelor'
        if level: matches=[o for o in options if level in question_key(o) and not re.search(r'\bor\b',question_key(o))]
    elif kind and kind.endswith('_month'):
        matches=[o for o in options if question_key(o)[:3]==norm[:3]]
    elif kind=='school':
        matches=[o for o in options if question_key(o).removeprefix('the ').replace(' at ',' ')==norm.removeprefix('the ').replace(' at ',' ')]
    elif kind=='major' and 'engineering' in norm:
        parts=['electrical engineering','computer engineering'] if norm=='electrical and computer engineering' else [norm]
        matches=[o for o in options if question_key(o) in parts]
        if not matches: matches=[o for o in options if question_key(o)=='engineering']
    elif kind=='graduation_date':
        month=date_component(str(value),'month');year=date_component(str(value),'year')
        if month and year:
            season='Spring' if month in ('January','February','March','April','May') else ('Summer' if month in ('June','July','August') else 'Fall')
            matches=[o for o in options if question_key(o) in (question_key(month+' '+year),question_key(season+' '+year))]
            if not matches:
                target=(int(year),list(calendar.month_name).index(month))
                for option in options:
                    parts=re.split(r'\s+[-–]\s+',option)
                    if len(parts)!=2: continue
                    bounds=[(date_component(p,'year'),date_component(p,'month')) for p in parts]
                    if all(y and m for y,m in bounds):
                        start,end=[(int(y),list(calendar.month_name).index(m)) for y,m in bounds]
                        if start<=target<=end: matches.append(option)
    elif kind=='gpa':
        try:
            number=float(value)
            matches=[o for o in options if (m:=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*',o)) and min(float(m[1]),float(m[2]))<=number<=max(float(m[1]),float(m[2]))]
            if not matches:
                for option in options:
                    m=re.fullmatch(r'(below|above)\s+(\d+(?:\.\d+)?)',option,re.I)
                    if m and ((m[1].lower()=='below' and number<float(m[2])) or (m[1].lower()=='above' and number>float(m[2]))): matches.append(option)
        except ValueError: pass
    return matches[0] if len(matches)==1 else None


def combined_answers(specific, shared):
    result={**shared,**specific}
    # The shared profile is authoritative; application essays stay specific.
    result.update({k:v for k,v in shared.items() if k.startswith(PREFIX)})
    return result


def resolved_answer(label, options, profile, answers):
    known=known_answer(label,profile,answers)
    matched=match_option(label,known,options)
    if matched is not None: return matched
    explicit={question_key(k):v for k,v in answers.items()}.get(question_key(label))
    return match_option(label,explicit,options)
