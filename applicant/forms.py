"""Conservative public-form adapter for native Lever/Greenhouse fields.

Only exact profile aliases are filled automatically. Employer questions, legal
acknowledgments and demographic answers come from this application's owner.
Unsupported widgets, security checks and authentication are handed back.
"""
import hashlib
import re
from shared.applying import form_digest

# This runs in the isolated employer page, never in the dashboard.
INSPECT = r"""form => {
 const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const controls = [...form.querySelectorAll('input,select,textarea')];
 const fields = [], seen = new Set();
 for (const el of controls) {
   const type = el.type || el.tagName.toLowerCase();
   if (['hidden','submit','button','reset'].includes(type) || el.disabled) continue;
   if (!visible(el) && type !== 'file') continue;
   const group = type === 'radio' ? controls.filter(e=>e.type==='radio' && e.name===el.name) : [el];
   const identity = type === 'radio' ? 'radio:' + el.name : el;
   if (seen.has(identity)) continue;
   seen.add(identity);
   const labels = [...(el.labels || [])].map(l=>l.innerText.trim()).filter(Boolean);
   const parent = el.closest('fieldset,.application-question,.field,.input-wrapper');
   const heading = parent?.querySelector('legend,.application-label,label');
   let label = (type === 'radio' ? heading?.innerText : labels.join(' ')) ||
      el.getAttribute('aria-label') || heading?.innerText || el.name || el.id;
   if (type === 'checkbox' && heading && !label.includes(heading.innerText.trim())) label = heading.innerText.trim() + ' — ' + label;
   label = (label || '').replace(/\s+/g,' ').trim();
   const i = fields.length;
   group.forEach(e=>e.setAttribute('data-radar-field', String(i)));
   const options = type === 'select-one' ? [...el.options].filter(o=>!o.disabled && o.value!=='').map(o=>({value:o.value,label:o.text.trim()})) :
      type === 'radio' ? group.map(e=>({value:e.value,label:[...(e.labels||[])].map(l=>l.innerText.trim()).join(' ') || e.value})) : [];
   fields.push({index:i,name:el.name,id:el.id,label,type,options,
      required:group.some(e=>e.required || e.getAttribute('aria-required')==='true') || /[\*✱∗]|\brequired\b/i.test(label),
      unsupported:el.getAttribute('role')==='combobox' || !!el.getAttribute('aria-autocomplete') || type==='select-multiple',
      maxlength:el.maxLength > 0 ? Math.min(el.maxLength,5000) : 5000});
 }
 return fields;
}"""

ALIASES = {
    "first name": "first_name", "given name": "first_name", "last name": "last_name",
    "family name": "last_name", "full name": "full_name", "name": "full_name",
    "email": "email", "email address": "email", "phone": "phone", "phone number": "phone",
    "current location": "location", "linkedin": "linkedin", "linkedin profile": "linkedin",
    "linkedin url": "linkedin", "github": "github", "github url": "github",
    "portfolio": "website", "portfolio url": "website", "website": "website", "personal website": "website",
}


def normalize(label):
    return re.sub(r"[^a-z0-9 ]", "", label.lower()).replace(" required", "").strip()


def enrich(fields, profile, answers):
    result = []
    for field in fields:
        # Changed labels/options create a new question, invalidating stale answers.
        signature = repr((field["name"], field["id"], field["label"], field["type"], field["options"]))
        field["key"] = hashlib.sha256(signature.encode()).hexdigest()[:32]
        field["value"] = ""
        field["answered"] = False
        if field["type"] == "file":
            field["resume"] = bool(re.search(r"\b(resume|résumé|cv)\b", field["label"], re.I)) or field["name"] in ("resume", "job_application[resume]")
            field["answered"] = field["resume"]
        elif field["key"] in answers:
            field["value"], field["answered"] = answers[field["key"]], True
        elif field["type"] in ("text", "email", "tel", "url", "textarea"):
            attr = ALIASES.get(normalize(field["label"]))
            value = (profile.get("first_name", "") + " " + profile.get("last_name", "")).strip() if attr == "full_name" else profile.get(attr, "")
            if value:
                field["value"], field["answered"] = value, True
        result.append(field)
    return result


class Handoff(Exception):
    pass


class PublicForm:
    def __init__(self, page, provider):
        self.page, self.provider = page, provider
        self.initial_text = page.locator("body").inner_text()
        self.form = page.locator("form#application-form,form#application_form")
        if self.form.count() != 1:
            raise Handoff("This employer's form layout needs manual completion. Open its application page.")
        if page.locator('iframe[src*="captcha"]:visible,input[type="password"]:visible').count():
            raise Handoff("The employer requires a CAPTCHA or account sign-in. Complete the application on its site.")

    def inspect(self, profile, answers):
        fields = enrich(self.form.evaluate(INSPECT), profile, answers)
        if not fields or not any(f.get("resume") for f in fields):
            raise Handoff("The resume upload could not be identified safely. Complete this form on the employer site.")
        checkbox_names = [f["name"] for f in fields if f["type"] == "checkbox"]
        if len(checkbox_names) != len(set(checkbox_names)):
            raise Handoff("This form has a multiple-selection checkbox question. Complete it on the employer site.")
        if len({f["key"] for f in fields}) != len(fields):
            raise Handoff("This form contains questions that could not be distinguished safely. Complete it on the employer site.")
        if len(fields) > 90 or any(not f["label"] or len(f["label"]) > 1800 for f in fields):
            raise Handoff("The application has questions that need review on the employer site.")
        custom_controls = self.form.locator('[role="combobox"],[role="listbox"],[contenteditable="true"]').evaluate_all(
            "els => els.some(e => !(e.matches('.select2-selection[role=combobox]') && e.closest('.application-field')?.querySelector('select.select2-hidden-accessible')))")
        if any(f["unsupported"] or len(f["options"]) > 4000 for f in fields) or custom_controls:
            raise Handoff("This form uses custom dropdowns or a multi-step control. Complete it on the employer site.")
        return fields

    def fill(self, fields, resume):
        # Finish resume parsing before applying explicit owner answers; parsing can
        # otherwise replace the chosen email/name after they have been filled.
        for f in fields:
            if f.get("resume"):
                self.form.locator(f'[data-radar-field="{f["index"]}"]').set_input_files(
                    {"name": resume.filename, "mimeType": "application/pdf", "buffer": resume.content})
                self.page.wait_for_load_state("networkidle", timeout=20000)
        for f in fields:
            loc = self.form.locator(f'[data-radar-field="{f["index"]}"]')
            if f.get("resume"):
                continue
            elif not f["answered"]:
                continue
            elif f["type"] in ("select-one", "radio"):
                if f["value"]:
                    if f["type"] == "radio":
                        options = f["options"]
                        i = next(i for i,o in enumerate(options) if o["value"] == f["value"])
                        loc.nth(i).check()
                    else:
                        loc.select_option(f["value"], force=True)
            elif f["type"] == "checkbox":
                loc.set_checked(f["value"] == "yes")
            elif f["type"] != "file":
                loc.fill(f["value"])

    def verify_values(self, fields):
        for f in fields:
            if not f["answered"] or f["type"] == "file":
                continue
            loc = self.form.locator(f'[data-radar-field="{f["index"]}"]')
            if f["type"] == "radio":
                actual = loc.evaluate_all("els => els.find(e=>e.checked)?.value || ''")
            elif f["type"] == "checkbox":
                actual = "yes" if loc.is_checked() else "no"
                if not f["value"]:
                    continue
            else:
                actual = loc.input_value()
            if actual != f["value"]:
                raise Handoff("The employer changed or reformatted an answer after filling. Check the form on its site before submitting.")

    def validation_errors(self):
        return self.form.locator('input:invalid:not([type="hidden"]),select:invalid,textarea:invalid').evaluate_all(
            "els => els.map(e=>({index:Number(e.getAttribute('data-radar-field')),message:e.validationMessage}))")

    def submit_button(self):
        selector = 'button[type="submit"],input[type="submit"]'
        if self.provider == "lever":
            selector += ',button#btn-submit[type="button"]'
        buttons = self.form.locator(selector)
        if buttons.count() != 1 or not buttons.is_visible() or not buttons.is_enabled():
            raise Handoff("The final Submit button is unavailable. Complete this form on the employer site.")
        label = buttons.inner_text() if buttons.evaluate("e=>e.tagName") == "BUTTON" else buttons.get_attribute("value")
        if not re.fullmatch(r"\s*(submit( application)?|send application|apply( for this job)?)\s*", label or "", re.I):
            raise Handoff("This form has another step before submission. Continue on the employer site.")
        return buttons

    def confirmation(self):
        # Must disappear after clicking; job descriptions cannot count as receipts.
        self.page.wait_for_function("""() => {
          const f=document.querySelector('form#application-form,form#application_form');
          return !f || !(f.offsetWidth || f.offsetHeight || f.getClientRects().length);
        }""", timeout=20000)
        text = self.page.locator("body").inner_text()
        patterns = [r"Your application (?:has been|was) (?:successfully )?(?:submitted|received)",
                    r"Thank you for (?:your interest|applying)[.!]", r"Application submitted[.!]?"]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match and not re.search(pattern, self.initial_text, re.I):
                return match.group(0)
        return None
