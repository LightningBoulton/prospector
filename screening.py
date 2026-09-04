#!/usr/bin/env python3
"""
screening.py — deterministic job-description screening, applied BEFORE and AFTER the model.

WHY THIS EXISTS
---------------
The model is good at judgment and bad at guarantees. Four of the reported problems are
guarantees, not judgments:

  * A role that requires the reader to personally be a software engineer, architect, data
    engineer or ML engineer must never reach the top of the email — no matter how much
    transformation language its description also carries, and no matter how the title reads
    ("Program Manager", "AI Transformation Lead", "Senior Forward Deployed Architect").
  * A hard, unsupported requirement in the JD — five years of full-cycle recruiting, a
    required license, SQL, CRM administration, dedicated RevOps — must OUTRANK generic
    strategy/transformation/leadership overlap, not be averaged against it.
  * A recruiting or talent role must be judged on its actual duties, not excluded on title
    history, and when it IS promising the specific execution gap must be named.
  * "Remote" in a location label is a label, not an eligibility statement. Whether a role is
    genuinely nationwide-US remote can only come from the JD.

Every rule here is EXPLICIT, word-boundary matched, and returns the evidence it fired on, so
any downgrade can be explained after the fact. Nothing here calls the network or the model.

TWO KINDS OF RULE, AND THE DIFFERENCE MATTERS
  * `technical_screen` can EXCLUDE — before a role is ever scored, so it costs nothing.
  * `hard_requirements` can only DOWNGRADE — it runs after scoring and caps a role at
    `stretch`. Downgrading keeps a role visible with its caveat attached; excluding it would
    re-create the false negatives broad discovery exists to remove.

CONFIGURATION
Every list below is a DEFAULT. A profile overrides any of them under `technical_exclusion`
or `hard_requirements` in profiles.json. A profile carrying NEITHER block is not screened at
all — which is how Chad's report stays byte-for-byte unchanged.

STEMS ARE DEAD TERMS. Matching is `\\bterm\\b` throughout, exactly like every other list in
this project, so "recruit" does not match "recruiting" and "certif" matches nothing at all.
Write complete words. `test_jobmonitor.py` enforces this for the committed config.
"""

import re

# ---- shared matching -----------------------------------------------------------------------

_WINDOW = 140          # chars either side of a token in which a requirement cue counts


def _rx(term):
    return re.compile(r"\b" + re.escape(term.lower()) + r"\b")


def _any_term(text, terms):
    t = (text or "").lower()
    return next((term for term in terms if _rx(term).search(t)), None)


def _all_hits(text, terms):
    t = (text or "").lower()
    return [term for term in terms if _rx(term).search(t)]


def _blob(posting):
    """Everything the JD actually says. Deliberately NOT truncated to DESC_LIMIT: the model
    sees the first 2000 characters, but a hard requirement is just as binding on page two."""
    return ((posting.get("description") or "") + " "
            + (posting.get("_detail_description") or "")).lower()


# ---- 1. technical-role exclusion (requirement 2) --------------------------------------------
#
# Titles that name a role the reader would have to BE a technical practitioner to hold.
# These exclude outright, before scoring.
#
# Read the omissions as carefully as the entries — several are load bearing:
#   * "integration" is NOT here and must never be. M&A integration, post-merger integration
#     and the integration management office are among the reader's strongest domains; only
#     the engineering compounds ("integration engineer") are excluded.
#   * "platform", "automation", "data" and "ai" are NOT here on their own. "Platform
#     Operations", "AI Adoption", "Automation Enablement" and "Data Governance" are
#     business-side roles; only their engineering compounds are excluded.
#   * "technical" on its own is NOT here — "Technical Program Manager" and "Technical
#     Architect" are, because those two specifically require engineering fluency to hold.
TECHNICAL_TITLE_TERMS = [
    # software engineering
    "software engineer", "software engineering", "software developer", "developer",
    "programmer", "frontend engineer", "front-end engineer", "backend engineer",
    "back-end engineer", "full stack engineer", "fullstack engineer", "web developer",
    "mobile engineer", "ios engineer", "android engineer", "embedded engineer",
    "firmware engineer", "principal engineer", "staff engineer", "engineering manager",
    "director of engineering", "head of engineering", "vp of engineering",
    # architecture / infrastructure
    "architect", "architects", "architecture", "solutions architect", "solution architect",
    "enterprise architect", "technical architect", "cloud architect", "data architect",
    "forward deployed", "forward-deployed", "infrastructure engineer", "platform engineer",
    "platform engineering", "systems engineer", "network engineer", "site reliability",
    "sre", "devops", "devsecops", "cloud engineer", "database administrator", "dba",
    # APIs / integrations
    "api engineer", "apis engineer", "integration engineer", "integrations engineer",
    "integration developer", "api developer", "sdk engineer", "solutions engineer",
    "sales engineer", "implementation engineer", "technical implementation",
    # data engineering
    "data engineer", "data engineering", "analytics engineer", "bi developer",
    "etl developer", "data platform engineer", "database engineer",
    # ML / AI engineering
    "machine learning engineer", "ml engineer", "mlops", "ai engineer",
    "applied scientist", "research scientist", "data scientist", "deep learning",
    # automation development
    "automation engineer", "automation developer", "rpa developer", "test engineer",
    "qa engineer", "sdet",
    # technical delivery roles that require engineering fluency to hold
    "technical program manager", "technical product manager", "technical project manager",
    "technical delivery manager", "security engineer", "cybersecurity engineer",
]

# Phrases in a DESCRIPTION that say the reader would personally have to do engineering work.
# Two are required (see `min_description_hits`) because any one of them can appear in a
# business-side JD describing the team the role works WITH rather than the work it does.
TECHNICAL_DESCRIPTION_TERMS = [
    "write code", "writing code", "coding experience", "hands-on coding",
    "production code", "code reviews", "software development experience",
    "software engineering experience", "degree in computer science",
    "bs in computer science", "computer science degree",
    "build apis", "design apis", "api development", "restful apis", "rest apis",
    "microservices", "system design", "technical architecture", "solution architecture",
    "reference architecture", "infrastructure as code", "terraform", "kubernetes",
    "ci/cd pipelines", "docker containers",
    "build data pipelines", "data pipelines", "etl pipelines", "data modeling",
    "write sql", "advanced sql", "sql queries", "python scripting", "shell scripting",
    "machine learning models", "train models", "model training", "model deployment",
    "feature engineering", "prompt engineering pipelines",
    "debug", "debugging", "troubleshoot code", "technical implementation experience",
]

# Business-side AI and transformation language. Its ONLY job is to stop a description-level
# technical signal from EXCLUDING a role outright — the role is still flagged, and
# `hard_requirements` still caps it at `stretch`. It never rescues a technical TITLE.
#
# This is the concrete form of "continue finding business-side AI roles": a JD about AI
# adoption that mentions model deployment twice describes the technology the organization is
# adopting, not the work the reader would be doing.
BUSINESS_OVERRIDE_TERMS = [
    "ai adoption", "ai enablement", "ai transformation", "ai literacy", "ai governance",
    "responsible ai", "ai strategy", "business-side ai", "workforce transformation",
    "change management", "change enablement", "organizational change",
    "organizational design", "organizational effectiveness", "operating model",
    "target operating model", "process redesign", "process improvement",
    "operational excellence", "business transformation", "enterprise transformation",
    "transformation office", "adoption strategy", "organizational adoption",
    "value realization", "stakeholder alignment", "executive alignment",
]


def _cfg(profile, block, key, default):
    cfg = (profile or {}).get(block) or {}
    value = cfg.get(key)
    return default if value is None else value


def technical_screening_enabled(profile):
    return bool((profile or {}).get("technical_exclusion", {}).get("enabled"))


def technical_screen(posting, profile):
    """Would this role require the READER to personally be a technical practitioner?

    Returns {"excluded", "reason", "risk", "evidence"}:
      excluded=True  — drop before scoring. A technical TITLE, or a technical description
                       with no business-side mandate to explain it.
      risk=True      — keep, but the role can never be promoted above `stretch`
                       (`hard_requirements` enforces that). This is the business-side AI role
                       whose JD is thick with technology it is adopting.
    """
    if not technical_screening_enabled(profile):
        return {"excluded": False, "reason": "", "risk": False, "evidence": []}
    title = (posting.get("title") or "").lower()
    title_terms = _cfg(profile, "technical_exclusion", "title_any", TECHNICAL_TITLE_TERMS)
    hit = _any_term(title, title_terms)
    if hit:
        return {"excluded": True, "risk": True, "evidence": [hit],
                "reason": f"technical role: title names '{hit}'"}


    desc_terms = _cfg(profile, "technical_exclusion", "description_any",
                      TECHNICAL_DESCRIPTION_TERMS)
    min_hits = int(_cfg(profile, "technical_exclusion", "min_description_hits", 2))
    hits = _all_hits(_blob(posting), desc_terms)
    if len(hits) < min_hits:
        return {"excluded": False, "reason": "", "risk": False, "evidence": []}

    override_terms = _cfg(profile, "technical_exclusion", "business_override_any",
                          BUSINESS_OVERRIDE_TERMS)
    overrides = _all_hits(_blob(posting), override_terms)
    if overrides:
        # `reason` is what the reader sees on the card, so it is written as the gap it is
        # rather than as a description of the rule that fired.
        return {"excluded": False, "risk": True, "evidence": hits[:3],
                "reason": (f"the posting asks for hands-on technical work "
                           f"({', '.join(hits[:3])})"),
                "note": (f"kept for its business mandate ({', '.join(overrides[:2])}), "
                         f"capped because of the technical ask")}
    return {"excluded": True, "risk": True, "evidence": hits[:3],
            "reason": (f"technical role: description requires "
                       f"{', '.join(hits[:3])} with no business-side mandate")}


# ---- 2. hard requirements (requirement 3) ---------------------------------------------------
#
# Each rule is (category, human label, tokens). A rule fires only when one of its tokens
# appears WITHIN `_WINDOW` characters of a requirement cue — "5 years", "must have",
# "required", "proven", "expert in". Without that proximity test every JD that mentions
# Salesforce in passing would downgrade, and the section would stop meaning anything.
REQUIREMENT_CUES = [
    "required", "requires", "must have", "must possess", "minimum of", "minimum",
    "at least", "years of", "years in", "years experience", "years of experience",
    "proven", "demonstrated", "expert", "expertise in", "deep experience",
    "extensive experience", "strong background", "proficiency", "proficient",
    "hands-on experience", "track record of", "you have", "you bring", "qualifications",
]

HARD_REQUIREMENT_RULES = [
    ("specialist_years", "specialist tenure in another function", [
        "full-cycle recruiting", "full cycle recruiting", "technical recruiting",
        "sales operations", "revenue operations", "revops", "marketing operations",
        "demand generation", "supply chain planning", "procurement", "underwriting",
        "actuarial", "clinical operations", "investment banking",
    ]),
    ("industry_specialization", "required industry specialization", [
        "healthcare industry experience", "life sciences experience",
        "pharmaceutical experience", "banking experience", "insurance industry experience",
        "defense industry", "aerospace experience", "semiconductor experience",
        "oil and gas experience", "public sector experience", "government contracting",
        "fintech experience", "biotech experience", "gaming industry experience",
    ]),
    ("license_or_certification", "required license or certification", [
        "pmp certification", "cpa", "shrm-cp", "shrm-scp", "phr", "sphr", "series 7",
        "cissp", "cisa", "cism", "rn license", "licensed clinician", "professional license",
        "six sigma black belt", "certified scrum master", "safe certification",
        "itil certification", "board certified",
    ]),
    ("sql_or_programming", "SQL or programming proficiency", [
        "sql", "python", "javascript", "java", "scala", "r programming", "vba",
        "dax", "power query", "api scripting",
    ]),
    ("crm_martech_admin", "CRM / MarTech administration", [
        "salesforce administrator", "salesforce admin", "administer salesforce",
        "crm administration", "hubspot administration", "marketo", "pardot",
        "eloqua", "marketing automation platform administration", "netsuite administration",
        "workday administration", "system administration",
    ]),
    ("engineering_architecture", "engineering or architecture depth", [
        "software architecture", "solution architecture", "technical architecture",
        "systems architecture", "cloud architecture", "software engineering",
        "distributed systems", "microservices architecture",
    ]),
    ("dedicated_revops", "dedicated RevOps / Sales Ops ownership", [
        "pipeline forecasting", "territory planning", "quota setting", "quota attainment",
        "commission planning", "sales compensation design", "deal desk",
        "sales forecasting cadence", "carrying a quota", "quota-carrying",
    ]),
    ("saas_renewals", "deep SaaS renewals / forecasting", [
        "renewal forecasting", "net revenue retention", "gross revenue retention",
        "churn forecasting", "book of business", "renewals quota", "upsell quota",
        "expansion quota",
    ]),
    ("full_cycle_recruiting", "full-cycle recruiting execution", [
        "full-cycle recruiting", "full cycle recruiting", "sourcing candidates",
        "candidate sourcing", "boolean search", "offer negotiation", "closing candidates",
        "requisition load", "req load", "requisition ownership", "high-volume recruiting",
        "ats execution", "phone screens",
    ]),
    ("manufacturing_production", "manufacturing / production expertise", [
        "manufacturing operations", "production line", "shop floor", "plant operations",
        "lean manufacturing", "production planning", "bill of materials",
        "quality management system", "iso 9001", "gmp",
    ]),
]

# Categories the recruiting-substance rule reports separately (requirement 4). They are a
# SUBSET of the rules above, not a second copy of them, so a change to the tokens lands in
# both places at once.
RECRUITING_EXECUTION_CATEGORIES = ("full_cycle_recruiting", "specialist_years")

# Title language that makes a role recruiting/talent-shaped. Used ONLY to decide whether the
# bridging-skills question applies — never to exclude (requirement 4 is explicit that
# recruiting roles must not be excluded on title history).
RECRUITING_TITLE_TERMS = [
    "recruiting", "recruiter", "recruitment", "talent acquisition", "talent",
    "sourcing", "hiring", "workforce planning", "people operations",
]


def _cue_near(text, start, end):
    window = text[max(0, start - _WINDOW):end + _WINDOW]
    return _any_term(window, REQUIREMENT_CUES)


def hard_requirements(posting, profile=None):
    """Material requirements the JD states that the reader does not support.

    Returns a list of {category, label, evidence, cue}. Empty when the JD is silent, which is
    the common case for the title-only sources (Workday, SmartRecruiters) — silence is not
    evidence of a clean fit, and `deep_retrieval_wanted` exists to reduce it."""
    rules = (profile or {}).get("hard_requirements", {}).get("rules")
    rules = HARD_REQUIREMENT_RULES if rules is None else [
        (r["category"], r.get("label", r["category"]), r["tokens"]) for r in rules]
    text = _blob(posting)
    if not text.strip():
        return []
    found, seen = [], set()
    for category, label, tokens in rules:
        for token in tokens:
            m = _rx(token).search(text)
            if not m:
                continue
            cue = _cue_near(text, m.start(), m.end())
            if not cue or category in seen:
                continue
            seen.add(category)
            found.append({"category": category, "label": label, "evidence": token,
                          "cue": cue})
            break
    return found


def is_recruiting_role(posting, profile=None):
    terms = (profile or {}).get("recruiting_terms") or RECRUITING_TITLE_TERMS
    return bool(_any_term((posting.get("title") or "").lower(), terms))


def recruiting_gaps(posting, profile=None):
    """The direct-execution gaps to FLAG on a recruiting/talent role (requirement 4).

    Returns [] for a role that is not recruiting-shaped, so a transformation role that
    happens to mention sourcing is not annotated as a recruiting role."""
    if not is_recruiting_role(posting, profile):
        return []
    return [r for r in hard_requirements(posting, profile)
            if r["category"] in RECRUITING_EXECUTION_CATEGORIES]


# ---- 3. remote eligibility from the JD (requirement 7) ---------------------------------------
#
# Scopes, most permissive first. A location LABEL never sets any of these — only the JD text
# does, which is the whole point of the requirement.
SCOPE_NATIONWIDE = "nationwide_us"        # genuinely open anywhere in the US
SCOPE_STATE_RESTRICTED = "state_restricted"
SCOPE_RESIDENCE_REQUIRED = "residence_required"
SCOPE_PAY_ZONE = "pay_zone"               # the state in the label is a pay band, not a rule
SCOPE_OFFICE_ASSOCIATED = "office_associated"
SCOPE_PREFERRED_LOCATION = "preferred_location"
SCOPE_UNCLEAR = "unclear"                 # the honest default

# Order matters: a JD that says both "must reside in California" and "remote within the US"
# is state-restricted, so restrictions are tested before the permissive phrases.
_SCOPE_RULES = [
    (SCOPE_STATE_RESTRICTED, [
        "residents of the following states", "eligible to work in the following states",
        "we can currently hire in", "we are able to hire in", "approved states",
        "registered to hire in", "hire in the following states",
        "must reside in one of", "only able to hire in", "states where we operate",
    ]),
    (SCOPE_RESIDENCE_REQUIRED, [
        "must reside in", "must live in", "must be a resident of",
        "required to live within", "commutable distance", "must be located in",
        "must be based in", "residency requirement",
    ]),
    (SCOPE_OFFICE_ASSOCIATED, [
        "days per week in the office", "days a week in the office", "days in office",
        "days onsite", "in-office", "assigned to our", "report to our office",
        "hybrid schedule", "hub location", "onsite presence",
    ]),
    (SCOPE_PAY_ZONE, [
        "pay zone", "compensation zone", "geographic differential",
        "salary range varies by location", "pay is based on location",
        "compensation varies by location", "geographic pay", "location-based pay",
    ]),
    # Broadened against how US remote JDs actually phrase it. The restriction rules above are
    # tested FIRST, so a posting that says both "remote within the US" and "we can hire in CA
    # and WA" is still state_restricted — which is what lets this list be generous without
    # letting it over-claim. Deliberately NOT here: "authorized to work in the united states",
    # which is a work-authorization requirement, not a statement about where you may live.
    (SCOPE_NATIONWIDE, [
        "anywhere in the united states", "anywhere in the us", "anywhere in the u.s.",
        "anywhere within the united states", "anywhere in the country",
        "remote from anywhere in the united states", "remote anywhere in the us",
        "work from anywhere in the country", "work from anywhere in the us",
        "fully remote within the united states", "fully remote in the united states",
        "100% remote within the united states", "fully remote role in the united states",
        "remote within the united states", "remote within the us",
        "open to candidates in the united states",
        "open to candidates anywhere in the united states",
        "open to candidates located anywhere in the united states",
        "open to any us-based candidate", "us-based remote", "usa-based remote",
        "remote position in the united states", "remote role in the united states",
        "all 50 states", "any us state", "any state in the us",
        "nationwide remote", "remote nationwide",
    ]),
    (SCOPE_PREFERRED_LOCATION, [
        "preferred location", "preferably located", "ideally based",
        "location preference", "proximity preferred",
    ]),
]

_REMOTE_LABEL = re.compile(
    r"\b(remote|anywhere|distributed|work from home|wfh|virtual)\b")


def remote_eligibility(posting):
    """What the JD — not the location label — actually says about where the reader may live.

    Returns {"labelled_remote", "scope", "evidence", "verified"}. `verified` is True only for
    SCOPE_NATIONWIDE: that is the single claim the report is allowed to make on the reader's
    behalf, and it may only be made when the JD says it. Everything else, including a JD that
    says nothing at all, is unverified — so "Remote — California" is reported as remote with
    unverified eligibility rather than as a nationwide-remote role."""
    loc = (posting.get("location") or "").lower()
    labelled = bool(_REMOTE_LABEL.search(loc))
    text = _blob(posting)
    if not text.strip():
        return {"labelled_remote": labelled, "scope": SCOPE_UNCLEAR, "evidence": "",
                "verified": False}
    for scope, phrases in _SCOPE_RULES:
        hit = _any_term(text, phrases)
        if hit:
            return {"labelled_remote": labelled, "scope": scope, "evidence": hit,
                    "verified": scope == SCOPE_NATIONWIDE}
    return {"labelled_remote": labelled, "scope": SCOPE_UNCLEAR, "evidence": "",
            "verified": False}


REMOTE_SCOPE_LABEL = {
    SCOPE_NATIONWIDE: "Remote (US) — confirmed in the posting",
    SCOPE_STATE_RESTRICTED: "Remote — limited to specific states",
    SCOPE_RESIDENCE_REQUIRED: "Remote — residency requirement",
    SCOPE_PAY_ZONE: "Remote — the state named is a pay zone",
    SCOPE_OFFICE_ASSOCIATED: "Remote — tied to an office",
    SCOPE_PREFERRED_LOCATION: "Remote — location preferred, not required",
    SCOPE_UNCLEAR: "Remote — eligibility not stated",
}


# ---- 4. deep JD retrieval gate (requirement 3, last line) -------------------------------------

def deep_retrieval_wanted(posting, profile):
    """Is this role promising enough to spend a per-posting detail fetch on?

    Requirement 3 says deep JD retrieval happens only on roles that are otherwise promising.
    A fetch costs one request and is the only per-posting network call besides salary
    enrichment, so the bar is: we do not already have the JD, and the role reached us on a
    strong signal — the precise title gate, an employer the reader named, or an SEO/content
    role, which requirement 8 asks to surface without demanding transformation language.

    Everything the deep fetch feeds — the technical screen, hard requirements, remote
    eligibility — is useless without a description, and the title-only sources (Workday,
    SmartRecruiters, Paradox) are exactly where the reader's priority employers live."""
    if (posting.get("description") or "").strip():
        return False
    if not posting.get("_detail_url") and not posting.get("url"):
        return False
    return bool(posting.get("tier") == "core"
                or posting.get("priority_employer")
                or is_seo_content(posting, profile))


# ---- 5. SEO / content coverage (requirement 8) -------------------------------------------------
#
# Named separately from the discovery title list because requirement 8 asks two things of
# these roles that the rest of the search does not: they must be retrieved WITHOUT
# transformation language, and the pre-send QA must confirm strong ones were included.
SEO_CONTENT_TERMS = [
    "seo", "search engine optimization", "enterprise seo", "technical seo",
    "organic search", "organic growth", "organic traffic", "search strategy",
    "seo content strategy", "content strategy", "content operations",
    "digital content strategy", "content marketing", "editorial strategy",
    "generative engine optimization", "answer engine optimization", "geo", "aeo",
    "ai search", "search experience",
]


def is_seo_content(posting, profile=None):
    terms = ((profile or {}).get("discovery") or {}).get("seo_content_any") \
        or SEO_CONTENT_TERMS
    return bool(_any_term((posting.get("title") or "").lower(), terms))
