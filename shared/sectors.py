"""Sector taxonomy and keyword-based tagging.

Deliberately rule-based (no LLM, no API cost). Each sector maps to keywords matched
against the posting title, description and any category hint the source provides.
Multi-word keywords match as substrings; single tokens match on word boundaries so
"ic" doesn't fire on "logistics".
"""

import re

SECTORS = {
    "software_tech": {
        "label": "Software / Tech",
        "keywords": [
            "software engineer", "software engineering", "software developer",
            "software development", "software design", "application developer",
            "swe", "full stack", "fullstack", "front end", "frontend",
            "back end", "backend", "web developer", "mobile developer",
            "ios developer", "android developer", "devops", "site reliability",
            "platform engineer", "infrastructure engineer", "cloud engineer",
            "data engineer", "data science", "data scientist", "data analyst",
            "machine learning", "deep learning", "artificial intelligence",
            "computer vision", "nlp", "natural language processing",
            "ml engineer", "mlops", "ai engineer", "research scientist",
            "cybersecurity", "security engineer", "qa engineer", "test engineer",
            "information technology", "programmer", "developer",
        ],
        "tokens": ["python", "java", "javascript", "golang", "kubernetes", "ai", "ml"],
    },
    "semiconductor": {
        "label": "Semiconductor",
        "keywords": [
            "semiconductor", "asic", "integrated circuit", "ic design",
            "vlsi", "chip design", "chip designer", "silicon", "foundry",
            "wafer", "fabrication", "process engineer", "process integration",
            "yield engineer", "lithography", "etch", "deposition",
            "device engineer", "device physics", "analog design",
            "mixed signal", "mixed-signal", "rf design", "rfic",
            "physical design", "design verification", "dft",
            "design for test", "tapeout", "tape-out", "eda",
            "standard cell", "cmos", "transistor", "packaging engineer",
            "test engineer semiconductor", "characterization engineer",
        ],
        "tokens": ["fab", "spice", "cadence", "synopsys"],
    },
    "computer_architecture": {
        "label": "Computer Architecture",
        "keywords": [
            "computer architecture", "cpu architecture", "gpu architecture",
            "processor design", "microarchitecture", "micro-architecture",
            "soc design", "soc architect", "rtl design", "rtl engineer",
            "verilog", "systemverilog", "vhdl", "fpga", "hardware engineer",
            "digital design", "logic design", "memory subsystem",
            "cache coherence", "interconnect", "performance modeling",
            "hardware architect", "silicon architect", "isa",
            "instruction set", "accelerator design", "hardware accelerator",
            "embedded systems", "embedded engineer", "firmware engineer",
            "bare metal", "device driver", "arch intern", "architecture intern",
            "compute architecture", "silicon design", "asic design",
        ],
        "tokens": ["risc-v", "arm", "asic", "hdl", "uvm", "cpu", "gpu", "npu", "soc"],
    },
    "power_electronics": {
        "label": "Power Electronics",
        "keywords": [
            "power electronics", "power converter", "power conversion",
            "dc-dc", "dc/dc", "ac-dc", "ac/dc", "inverter", "rectifier",
            "motor drive", "motor control", "power supply", "smps",
            "switching regulator", "battery management", "bms",
            "energy storage", "power systems", "power engineer",
            "electrical engineer", "electrical engineering",
            "circuit design", "analog circuit", "pcb design",
            "magnetics", "transformer design", "gate driver",
            "power integrity", "signal integrity", "thermal management",
            "grid", "renewable energy", "solar", "wind energy",
            "electric vehicle", "ev charging", "powertrain", "traction",
        ],
        "tokens": ["igbt", "mosfet", "gan", "sic", "pwm", "hvdc"],
    },
    "hardware_general": {
        "label": "Hardware (general)",
        "keywords": [
            "hardware engineer", "hardware engineering", "hardware design",
            "electronics engineer", "electronic engineering",
            "board design", "schematic", "prototyping engineer",
            "validation engineer", "hardware validation", "hardware test",
            "systems engineer hardware", "mechanical engineer",
            "manufacturing engineer", "product development engineer",
            "instrumentation", "sensors", "optics", "photonics",
            "rf engineer", "antenna", "electromagnetics",
        ],
        "tokens": ["pcb", "cad", "solidworks", "altium"],
    },
    "robotics": {
        "label": "Robotics",
        "keywords": [
            "robotics", "robotic", "robot", "autonomous systems",
            "autonomous vehicle", "self-driving", "self driving",
            "mechatronics", "motion planning", "path planning",
            "control systems", "controls engineer", "control engineer",
            "guidance navigation", "gnc", "state estimation",
            "sensor fusion", "slam", "localization", "manipulation",
            "kinematics", "dynamics", "actuator", "servo",
            "drone", "uav", "unmanned", "perception engineer",
            "lidar", "computer vision robotics", "teleoperation",
            "human robot interaction", "swarm", "automation engineer",
            "industrial automation", "plc",
        ],
        "tokens": ["ros", "ros2", "moveit", "gazebo"],
    },
}

# Category strings that upstream sources (e.g. SimplifyJobs) already provide,
# mapped to our taxonomy so we inherit their curation instead of re-deriving it.
CATEGORY_HINT_MAP = {
    "software engineering": ["software_tech"],
    "ai/ml/data": ["software_tech"],
    "data science": ["software_tech"],
    "product management": [],
    "quantitative finance": [],
    # A bare "Hardware" category is too coarse to imply a specific discipline -
    # mapping it to all three would put chip-design roles in a power-electronics
    # filter. It falls back to the general bucket instead.
    "hardware": ["hardware_general"],
    "hardware engineering": ["hardware_general"],
    "other": [],
}

# Terms that indicate an internship/co-op rather than a full-time role.
INTERNSHIP_MARKERS = [
    "intern", "internship", "co-op", "coop", "co op",
    "summer analyst", "student worker", "undergraduate research",
    "industrial placement", "placement year", "apprentice",
    "trainee", "工作实习",
]

# Terms that indicate a role we should NOT treat as an internship.
NON_INTERNSHIP_MARKERS = [
    "internal", "international",  # guard against substring false-positives on "intern"
]


def _normalize(text: str) -> str:
    """Fold separators to spaces so 'Full-Stack' and 'full stack' match alike."""
    return re.sub(r"\s+", " ", re.sub(r"[-_/(),.]+", " ", text or "")).strip()


def _compile(patterns):
    """Compile keywords into one anchored, case-insensitive pattern.

    Patterns are normalized the same way as the text they're matched against, so
    hyphenation differences between sources never cause a miss.

    Anchoring is asymmetric on purpose. The leading \b is strict, because without
    it "arch intern" matches inside "reseARCH INTERN" and mis-tags every research
    role. The trailing edge allows suffix characters, so one keyword covers its
    inflections ("firmware engineer" also matches "firmware engineerING").
    """
    normalized = [_normalize(p) for p in patterns]
    return re.compile(
        "|".join(rf"\b{re.escape(p)}\w*" for p in normalized),
        re.IGNORECASE,
    )


_SECTOR_PATTERNS = {
    key: _compile(cfg["keywords"] + cfg.get("tokens", []))
    for key, cfg in SECTORS.items()
}
_INTERNSHIP_PATTERN = _compile(INTERNSHIP_MARKERS)


def is_internship(title: str, description: str = "", term: str = "") -> bool:
    """True if the posting looks like an internship or co-op position."""
    haystack = _normalize(" ".join(filter(None, [title, term, description or ""])))
    # Strip words that merely contain "intern" as a substring before matching.
    for bad in NON_INTERNSHIP_MARKERS:
        haystack = re.sub(rf"\b{bad}\w*\b", " ", haystack, flags=re.IGNORECASE)
    return bool(_INTERNSHIP_PATTERN.search(haystack))


def tag_posting(title: str, description: str = "", category_hint: str = "") -> list:
    """Return the list of sector keys this posting belongs to (may be empty)."""
    haystack = _normalize(" ".join(filter(None, [title, description or ""])))
    tags = {key for key, pat in _SECTOR_PATTERNS.items() if pat.search(haystack)}

    # Only trust a source's own category when our keywords found nothing —
    # hardware hints in particular are too coarse to apply on their own.
    if not tags and category_hint:
        tags.update(CATEGORY_HINT_MAP.get(category_hint.strip().lower(), []))

    return sorted(tags)


def sector_labels() -> dict:
    return {key: cfg["label"] for key, cfg in SECTORS.items()}
