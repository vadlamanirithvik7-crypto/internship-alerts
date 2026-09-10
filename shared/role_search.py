"""Role identity comes from the title, not employer boilerplate or skill mentions."""
import re

ROLE_LABELS = {
    "software": "Software engineering",
    "embedded": "Embedded / firmware",
    "hardware": "Electrical / hardware / chips",
}
SECTOR_ROLES = {"software_tech": "software", "robotics": "hardware", "hardware_general": "hardware",
                "asic_design": "hardware", "semiconductor": "hardware", "computer_architecture": "hardware",
                "power_electronics": "hardware"}


def selected_roles(params):
    if params.get("roles_filter") or params.getlist("role"):
        return params.getlist("role")
    if params.getlist("sector"):
        return sorted({SECTOR_ROLES[s] for s in params.getlist("sector") if s in SECTOR_ROLES})
    return list(ROLE_LABELS)
ROLE_PATTERNS = {
    "software": r"\b(?:software|swe|full[ -]?stack|front[ -]?end|back[ -]?end|web develop|mobile develop|application(?:s)? (?:engineer|develop)|ios (?:engineer|develop)|android (?:engineer|develop)|devops|site reliability|cloud engineer|platform engineer|infrastructure engineer|data engineer|machine learning engineer|ai engineer|software quality|compiler engineer|api engineer|sdet)",
    "embedded": r"\b(?:embedded|firmware|device driver|bsp engineer|real[ -]?time software|microcontroller)",
    "hardware": r"\b(?:hardware|electrical|electronic|circuit|pcb|board design|asic|rtl|fpga|silicon|semiconductor|vlsi|soc\b|chip design|design verification|verification engineer|physical design|digital design|analog|mixed[ -]signal|rf engineer|rf design|computer architecture|microarchitecture|cpu|gpu|power electronics|power systems|controls engineer|control systems|robotics|mechatronics)",
}


def role_tags(title):
    title = (title or "").lower()
    if re.search(r"\b(?:sales|recruiting|recruiter|marketing|finance|financial|accounting|business operations|product manager|product management|supply chain|human resources|data analyst|customer support|technical support)\b", title):
        return []
    roles = [key for key, pattern in ROLE_PATTERNS.items() if re.search(pattern, title)]
    # Prefer the primary engineering discipline over a team/product qualifier.
    if "embedded" in roles:
        return ["embedded"]
    if "software" in roles:
        return ["software"]
    return roles
